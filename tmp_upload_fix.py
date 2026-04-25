#!/usr/bin/env python3
"""Fix remaining files: aor_func.h, makefile, redo_bthr.c"""
import paramiko

HOST = "192.168.1.174"
USER = "release"
PASS = "dsg@release"
BASE = "/release/release/release/xyb/test22/module/c/v4/aor"

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, password=PASS)
sftp = ssh.open_sftp()

# ============================================================
# 3. Modify aor_func.h — add declarations
# ============================================================
path_hdr = f"{BASE}/aor_func.h"
with sftp.open(path_hdr, 'r') as f:
    hdr_content = f.read().decode('utf-8')

decl_push = 'extern void     aor_trunc_queue_push(AOR *ar, int seq, const char *tfn, uint64_t wsz);'
decl_init = 'extern int      aor_trunc_queue_init(AOR *ar);'

if 'aor_trunc_queue_push' not in hdr_content:
    anchor = 'extern void     aor_redo_queue_push'
    idx = hdr_content.find(anchor)
    if idx >= 0:
        eol = hdr_content.find('\n', idx)
        insert_pos = eol + 1
        insert_text = f'\n{decl_push}\n{decl_init}\n'
        hdr_content = hdr_content[:insert_pos] + insert_text + hdr_content[insert_pos:]
        with sftp.open(path_hdr, 'w') as f:
            f.write(hdr_content.encode('utf-8'))
        print(f"[OK] modified {path_hdr} (added trunc declarations)")
    else:
        print(f"[WARN] anchor not found in {path_hdr}")
else:
    print(f"[SKIP] declarations already exist in {path_hdr}")

# ============================================================
# 4. Modify makefile — add func/trunc_bthr.o
# ============================================================
path_mk = f"{BASE}/makefile"
with sftp.open(path_mk, 'r') as f:
    mk_content = f.read().decode('utf-8')

if 'trunc_bthr.o' not in mk_content:
    anchor_mk = 'func/redo_bthr.o \\'
    idx_mk = mk_content.find(anchor_mk)
    if idx_mk >= 0:
        insert_after = idx_mk + len(anchor_mk)
        nl = mk_content.find('\n', insert_after)
        if nl < 0:
            nl = len(mk_content)
        line_start = mk_content.rfind('\n', 0, idx_mk)
        if line_start < 0:
            line_start = 0
        else:
            line_start += 1
        ws = ''
        for ch in mk_content[line_start:idx_mk]:
            if ch in (' ', '\t'):
                ws += ch
            else:
                break
        insert_line = f'\n{ws}func/trunc_bthr.o \\'
        mk_content = mk_content[:nl] + insert_line + mk_content[nl:]
        with sftp.open(path_mk, 'w') as f:
            f.write(mk_content.encode('utf-8'))
        print(f"[OK] modified {path_mk} (added trunc_bthr.o)")
    else:
        print(f"[WARN] anchor not found in {path_mk}")
else:
    print(f"[SKIP] trunc_bthr.o already in {path_mk}")

# ============================================================
# 5. Modify redo_bthr.c — add trunc_queue_init call
# ============================================================
path_rb2 = f"{BASE}/func/redo_bthr.c"
with sftp.open(path_rb2, 'r') as f:
    rb2_content = f.read().decode('utf-8')
rb2_lines = rb2_content.split('\n')

init_insert_done = False
for i, line in enumerate(rb2_lines):
    if 'is_init = 1;' in line and not init_insert_done:
        nearby = '\n'.join(rb2_lines[max(0,i-5):i+5])
        if 'aor_trunc_queue_init' in nearby:
            print(f"[SKIP] trunc_queue_init already in {path_rb2}")
            init_insert_done = True
            break

        indent2 = ''
        for ch in line:
            if ch in (' ', '\t'):
                indent2 += ch
            else:
                break

        insert_block = [
            '',
            f'{indent2}if (ar->par.flg & AOR_FLG_BACKU_USE_REDO)',
            f'{indent2}{{',
            f'{indent2}    if (aor_trunc_queue_init(ar) < 0)',
            f'{indent2}        GO_ERROR(err_1)',
            f'{indent2}}}',
        ]
        for j, ins_line in enumerate(insert_block):
            rb2_lines.insert(i + j, ins_line)

        new_content = '\n'.join(rb2_lines)
        with sftp.open(path_rb2, 'w') as f:
            f.write(new_content.encode('utf-8'))
        print(f"[OK] modified {path_rb2} (added trunc_queue_init before is_init=1)")
        init_insert_done = True
        break

if not init_insert_done:
    print(f"[WARN] could not find 'is_init = 1;' in {path_rb2}")

sftp.close()
ssh.close()
print("\n[DONE] All remaining files modified successfully.")
