#!/usr/bin/env python3
"""Binary hot-fix for the Open Fortress bot memory leaks in server.dll (32-bit).

Only applies to the exact build shipped with Open Fortress (server.dll built
2024-08-08 17:21:06 UTC, sha256 65a8180d...a041fa). Refuses to touch anything else.

Fixes (see TECHNICAL.md for the analysis):
  A. CBot::PlayerMove: before `m_lastCmd = m_cmd`, delete the previous command
     (if non-NULL and != m_cmd). Ends the one-`new CUserCmd`-per-bot-per-tick leak.
  B. CBotMemory::GetDataMemory(name, forceIfNotExists=true): instead of
     `return new CDataMemory()` on every miss (leaked by every caller of the
     GetDataMemoryInt/Vector/... macros), lazily build ONE default CDataMemory
     and return it every time. All callers only read from it.
  C. CBot::CBot: zero m_cmd/m_lastCmd at construction (defensive; makes A safe
     even if Update ever ran before the first CBot::Spawn).

All injected code is position independent (relative calls, PC-relative data
access), so no relocation entries are needed. Code goes in the zero tail of
.text's raw data; the 4-byte cache slot goes in the zero-filled tail of
.data's last page. Section VirtualSizes are bumped to cover both and the PE
checksum is recomputed.

usage: python -I patch_server_dll.py <in server.dll> <out server.dll> [--only ABC]
"""
import hashlib
import struct
import sys

import pefile

ORIG_SHA256 = "65a8180de5480115b317dc1b30a8f29295a1b7a22d6b2e6d7ab42a18dba041fa"
BASE = 0x10000000

# Addresses (VA at preferred base) and the bytes expected there.
PLAYERMOVE_SITE = 0x103077B7
PLAYERMOVE_ORIG = bytes.fromhex("8b4368894364")    # mov eax,[ebx+68h]; mov [ebx+64h],eax
GDM_SITE = 0x1031E593
GDM_ORIG = bytes.fromhex("6a3ce836aaecff")         # push 3Ch; call operator new
GDM_RESUME = 0x1031E5B2                            # epilogue returning eax (after the original ctor call)
CTOR_SITE = 0x10305A99
CTOR_ORIG = bytes.fromhex("c7461800000000")        # mov dword [esi+18h],0
CUSERCMD_DDTOR = 0x101B62B0                        # CUserCmd scalar deleting destructor (thiscall, flags arg, ret 4)
OP_NEW = 0x101E8FD0                                # operator new(size_t) -> g_pMemAlloc->Alloc
CDATAMEM_CTOR = 0x1031DC60                         # CDataMemory::CDataMemory()
EXPECT = {  # sanity checks on callees / surrounding code
    CUSERCMD_DDTOR: bytes.fromhex("558becf6450801568bf1c70614a55f10"),
    OP_NEW: bytes.fromhex("558beca168e25910"),
    CDATAMEM_CTOR: bytes.fromhex("558bec6aff6870185710"),
    GDM_RESUME: bytes.fromhex("8b4df464890d00000000"),
    GDM_SITE - 6: bytes.fromhex("807d0c007433"),   # cmp byte [ebp+0Ch],0 ; je null (forceIfNotExists)
}


def rel32(frm_next, to):
    return struct.pack("<i", to - frm_next)


def main():
    src, dst = sys.argv[1], sys.argv[2]
    only = "ABC"
    if "--only" in sys.argv:
        only = sys.argv[sys.argv.index("--only") + 1].upper()
    raw = bytearray(open(src, "rb").read())
    if hashlib.sha256(raw).hexdigest() != ORIG_SHA256:
        sys.exit("refusing: input is not the expected original server.dll")
    pe = pefile.PE(data=bytes(raw))
    assert pe.FILE_HEADER.Machine == 0x14C and pe.OPTIONAL_HEADER.ImageBase == BASE
    sec = {s.Name.rstrip(b"\0").decode(): s for s in pe.sections}
    text, data = sec[".text"], sec[".data"]

    def off(va):
        return pe.get_offset_from_rva(va - BASE)

    def expect(va, b):
        got = bytes(raw[off(va):off(va) + len(b)])
        if got != b:
            sys.exit("mismatch at %08x: %s != %s" % (va, got.hex(), b.hex()))

    for va, b in [(PLAYERMOVE_SITE, PLAYERMOVE_ORIG), (GDM_SITE, GDM_ORIG), (CTOR_SITE, CTOR_ORIG)] + list(EXPECT.items()):
        expect(va, b)

    # Cave in .text tail (between VirtualSize and SizeOfRawData; must be zeros).
    cave_rva = (text.VirtualAddress + text.Misc_VirtualSize + 15) & ~15
    cave_end_rva = text.VirtualAddress + text.SizeOfRawData
    # Cache slot in .data's final page, past VirtualSize (zero-filled by the loader).
    slot_rva = (data.VirtualAddress + data.Misc_VirtualSize + 15) & ~15
    assert slot_rva + 4 <= ((data.VirtualAddress + data.Misc_VirtualSize + 0xFFF) & ~0xFFF)
    assert slot_rva + 4 <= sec["_RDATA"].VirtualAddress
    slot_va = BASE + slot_rva

    code = bytearray()
    cur = BASE + cave_rva

    def emit(b):
        code.extend(b)

    def here():
        return cur + len(code)

    def pad():
        while len(code) % 16:
            emit(b"\xcc")

    # ---- cave A: called from CBot::PlayerMove (ebx = this) ----
    caveA = here()
    emit(bytes.fromhex("8b4364"))        # mov eax,[ebx+64h]   old m_lastCmd
    emit(bytes.fromhex("8b4b68"))        # mov ecx,[ebx+68h]   m_cmd
    emit(bytes.fromhex("894b64"))        # mov [ebx+64h],ecx   m_lastCmd = m_cmd
    emit(bytes.fromhex("85c0"))          # test eax,eax
    emit(bytes.fromhex("740d"))          # jz done
    emit(bytes.fromhex("3bc1"))          # cmp eax,ecx
    emit(bytes.fromhex("7409"))          # je done
    emit(bytes.fromhex("6a01"))          # push 1 (delete flag)
    emit(bytes.fromhex("8bc8"))          # mov ecx,eax
    emit(b"\xe8" + rel32(here() + 5, CUSERCMD_DDTOR))  # call CUserCmd scalar deleting dtor (ret 4)
    emit(bytes.fromhex("8b4368"))        # done: mov eax,[ebx+68h] (same eax as the original code)
    emit(b"\xc3")                        # ret
    pad()

    # ---- cave B: called from CBotMemory::GetDataMemory force path; returns eax ----
    caveB = here()
    emit(b"\xe8\x00\x00\x00\x00")        # call $+5
    L = here()
    emit(b"\x59")                        # pop ecx  (ecx = L)
    dG = struct.pack("<i", slot_va - L)
    emit(b"\x8b\x81" + dG)               # mov eax,[ecx+dG]
    emit(b"\x85\xc0")                    # test eax,eax
    jnz_at = len(code)
    emit(b"\x75\x00")                    # jnz done
    emit(b"\x56")                        # push esi
    emit(b"\x8b\xf1")                    # mov esi,ecx
    emit(b"\x6a\x3c")                    # push 3Ch (sizeof CDataMemory)
    emit(b"\xe8" + rel32(here() + 5, OP_NEW))
    emit(b"\x83\xc4\x04")                # add esp,4
    emit(b"\x85\xc0")                    # test eax,eax
    jz_at = len(code)
    emit(b"\x74\x00")                    # jz restore
    emit(b"\x89\x86" + dG)               # mov [esi+dG],eax
    emit(b"\x8b\xc8")                    # mov ecx,eax
    emit(b"\xe8" + rel32(here() + 5, CDATAMEM_CTOR))
    emit(b"\x8b\x86" + dG)               # mov eax,[esi+dG]
    restore = len(code)
    emit(b"\x5e")                        # pop esi
    done = len(code)
    emit(b"\xc3")                        # ret
    code[jnz_at + 1] = done - (jnz_at + 2)
    code[jz_at + 1] = restore - (jz_at + 2)
    pad()

    # ---- cave C: CBot::CBot (esi = this, eax must be preserved) ----
    caveC = here()
    emit(bytes.fromhex("c7461800000000"))  # mov dword [esi+18h],0  (the displaced original instruction)
    emit(bytes.fromhex("c7466400000000"))  # mov dword [esi+64h],0  m_lastCmd = NULL
    emit(bytes.fromhex("c7466800000000"))  # mov dword [esi+68h],0  m_cmd = NULL
    emit(b"\xc3")
    pad()

    assert cave_rva + len(code) <= cave_end_rva, "cave too large"
    co = text.PointerToRawData + (cave_rva - text.VirtualAddress)
    if any(raw[co:co + len(code)]):
        sys.exit("cave area not zero")
    raw[co:co + len(code)] = code

    if "A" in only:
        o = off(PLAYERMOVE_SITE)
        raw[o:o + 6] = b"\xe8" + rel32(PLAYERMOVE_SITE + 5, caveA) + b"\x90"
    if "B" in only:
        o = off(GDM_SITE)
        jmp = GDM_RESUME - (GDM_SITE + 7)
        assert -128 <= jmp < 128
        raw[o:o + 7] = b"\xe8" + rel32(GDM_SITE + 5, caveB) + b"\xeb" + struct.pack("<b", jmp)
    if "C" in only:
        o = off(CTOR_SITE)
        raw[o:o + 7] = b"\xe8" + rel32(CTOR_SITE + 5, caveC) + b"\x90\x90"

    # Bump VirtualSizes to cover the cave and the slot; recompute the checksum.
    pe2 = pefile.PE(data=bytes(raw))
    for s in pe2.sections:
        n = s.Name.rstrip(b"\0")
        if n == b".text":
            s.Misc_VirtualSize = cave_rva + len(code) - s.VirtualAddress
        elif n == b".data":
            s.Misc_VirtualSize = slot_rva + 4 - s.VirtualAddress
    out = bytearray(pe2.write())
    pe3 = pefile.PE(data=bytes(out))
    pe3.OPTIONAL_HEADER.CheckSum = pe3.generate_checksum()
    out = pe3.write()
    open(dst, "wb").write(out)
    print("caves: A=%08x B=%08x C=%08x slot=%08x applied=%s" % (caveA, caveB, caveC, slot_va, only))
    print("sha256", hashlib.sha256(out).hexdigest())


if __name__ == "__main__":
    main()
