#!/usr/bin/env python3
"""Upload trunc_bthr.c to remote server and modify related files."""
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
# 1. Create func/trunc_bthr.c
# ============================================================
trunc_content = r"""
/*
 * trunc_bthr.c — truncate task queue (independent)
 *
 * Push truncate requests into a queue; a background thread
 * periodically queries "select count(1)+1 from v$log" to
 * determine how many redo files to keep (N), then truncates
 * the oldest (total - N) files.
 */

#include "aor.h"
#include "aor_func.h"

/* ---- node & queue ---- */
typedef struct trunc_emb {
    int              seq;
    uint64_t         wsz;
    char             tfn[MAX_FILE_NAME_LEN+1];
    struct trunc_emb *next;
} trunc_emb_t;

typedef struct trunc_queue {
    uint8_t        *producer_block;
    size_t          producer_size;
    uint8_t        *ps_pb, *pse_pb;   /* scan / scan-end pointer */
    trunc_emb_t    *head, *tail;
    pthread_mutex_t mutex;
    AOR            *ar;
} trunc_queue_t;

static trunc_queue_t tq;

/* ---------------------------------------------------------- */
/* push (producer)                                            */
/* ---------------------------------------------------------- */
void
aor_trunc_queue_push(AOR *ar, int seq, const char *tfn, uint64_t wsz)
{
    trunc_emb_t *node;
    uint8_t     *new_block;
    size_t       new_size;

    pthr_lock(&tq.mutex);

    /* dedup — same seq already queued */
    for (node = tq.head; node; node = node->next)
    {
        if (node->seq == seq)
        {
            node->wsz = wsz;
            str_ncpy1(node->tfn, tfn, MAX_FILE_NAME_LEN);
            pthr_unlock(&tq.mutex);
            return;
        }
    }

    /* reuse freed slot (seq == -1) */
    if (tq.producer_block)
    {
        uint8_t *p = tq.ps_pb;
        while (p + sizeof(trunc_emb_t) <= tq.pse_pb)
        {
            node = (trunc_emb_t *)PTR_ALIGN(p, 8);
            if ((uint8_t *)node + sizeof(trunc_emb_t) > tq.pse_pb)
                break;
            if (node->seq == -1)
            {
                node->seq  = seq;
                node->wsz  = wsz;
                str_ncpy1(node->tfn, tfn, MAX_FILE_NAME_LEN);
                node->next = NULL;
                if (tq.tail) tq.tail->next = node;
                else         tq.head       = node;
                tq.tail = node;
                pthr_unlock(&tq.mutex);
                return;
            }
            p = (uint8_t *)node + sizeof(trunc_emb_t);
        }
    }

    /* allocate / expand block */
    if (!tq.producer_block)
    {
        tq.producer_size = 4096;
        IN_MALLOC_U8(tq.producer_block, tq.producer_size);
        MEMSET(tq.producer_block, 0, tq.producer_size);
        tq.ps_pb  = tq.producer_block;
        tq.pse_pb = tq.producer_block + tq.producer_size;
    }
    else
    {
        uint8_t *next_slot = tq.pse_pb;       /* would-be slot */
        if (tq.tail)
            next_slot = (uint8_t *)tq.tail + sizeof(trunc_emb_t);

        if (next_slot + sizeof(trunc_emb_t) > tq.pse_pb)
        {
            new_size  = tq.producer_size * 2;
            IN_MALLOC_U8(new_block, new_size);
            MEMSET(new_block, 0, new_size);
            memcpy(new_block, tq.producer_block, tq.producer_size);

            /* fix pointers */
            {
                ptrdiff_t delta = new_block - tq.producer_block;
                trunc_emb_t *n;
                if (tq.head) tq.head = (trunc_emb_t *)((uint8_t *)tq.head + delta);
                if (tq.tail) tq.tail = (trunc_emb_t *)((uint8_t *)tq.tail + delta);
                for (n = tq.head; n; n = n->next)
                {
                    if (n->next)
                        n->next = (trunc_emb_t *)((uint8_t *)n->next + delta);
                }
            }

            FREE(tq.producer_block);
            tq.producer_block = new_block;
            tq.producer_size  = new_size;
            tq.ps_pb  = new_block;
            tq.pse_pb = new_block + new_size;
        }
    }

    /* append new node at end of used area */
    {
        uint8_t *slot;
        if (tq.tail)
            slot = (uint8_t *)tq.tail + sizeof(trunc_emb_t);
        else
            slot = tq.producer_block;

        node = (trunc_emb_t *)PTR_ALIGN(slot, 8);
        node->seq  = seq;
        node->wsz  = wsz;
        str_ncpy1(node->tfn, tfn, MAX_FILE_NAME_LEN);
        node->next = NULL;

        if (tq.tail) tq.tail->next = node;
        else         tq.head       = node;
        tq.tail = node;
    }

    pthr_unlock(&tq.mutex);
    return;

err_1:
    pthr_unlock(&tq.mutex);
    cfg_eprint1(MODULE_AOR, "trunc_queue_push: malloc failed seq=%d", seq);
}

/* ---- sort helper ---- */
typedef struct {
    int      seq;
    uint64_t wsz;
    char     tfn[MAX_FILE_NAME_LEN+1];
} trunc_item_t;

static int
_trunc_cmp_seq(const void *a, const void *b)
{
    return ((const trunc_item_t *)a)->seq - ((const trunc_item_t *)b)->seq;
}

/* ---------------------------------------------------------- */
/* pop loop (consumer thread)                                 */
/* ---------------------------------------------------------- */
static void *
_trunc_pop_loop(void *arg)
{
    AOR         *ar = (AOR *)arg;
    int          keep_cnt, total, i, trunc_cnt, ret;
    trunc_emb_t *node;
    trunc_item_t *list = NULL;
    int           list_cap = 0;

    while (!ar->isexit)
    {
        sleep(20);
        if (ar->isexit)
            break;

        /* --- get keep count from DB --- */
        {
            OXAC *o = NULL;
            ret = oxac_pool_open(&ar->ocp);
            if (ret < 0)
            {
                cfg_eprint1(MODULE_AOR, "trunc_pop: pool_open failed ret=%d", ret);
                continue;
            }
            o = (OXAC *)ret;

            keep_cnt = oxac_svc_sql_count(o, "select count(1)+1 from v$log");

            oxac_pool_rclose(ret, &ar->ocp, o);

            if (keep_cnt <= 0)
            {
                cfg_eprint1(MODULE_AOR, "trunc_pop: invalid keep_cnt=%d", keep_cnt);
                continue;
            }
        }

        /* --- collect queue snapshot --- */
        pthr_lock(&tq.mutex);

        total = 0;
        for (node = tq.head; node; node = node->next)
        {
            if (node->seq >= 0)
                total++;
        }

        if (total <= keep_cnt)
        {
            pthr_unlock(&tq.mutex);
            continue;
        }

        /* ensure list buffer */
        if (total > list_cap)
        {
            if (list) free(list);
            list = (trunc_item_t *)malloc(sizeof(trunc_item_t) * total);
            if (!list)
            {
                list_cap = 0;
                pthr_unlock(&tq.mutex);
                cfg_eprint1(MODULE_AOR, "trunc_pop: malloc list failed");
                continue;
            }
            list_cap = total;
        }

        i = 0;
        for (node = tq.head; node; node = node->next)
        {
            if (node->seq >= 0)
            {
                list[i].seq = node->seq;
                list[i].wsz = node->wsz;
                str_ncpy1(list[i].tfn, node->tfn, MAX_FILE_NAME_LEN);
                i++;
            }
        }
        pthr_unlock(&tq.mutex);

        /* sort ascending by seq */
        qsort(list, total, sizeof(trunc_item_t), _trunc_cmp_seq);

        /* truncate oldest (total - keep_cnt) files */
        trunc_cnt = total - keep_cnt;

        for (i = 0; i < trunc_cnt; i++)
        {
            STB *s = ar->stb;
            int  fd;

            fd = s->sio.stb_open(s, list[i].tfn, O_RDWR|O_BINARY, 0644);
            if (fd < 0)
            {
                cfg_eprint1(MODULE_AOR,
                    "trunc_pop: open failed tfn=%s seq=%d",
                    list[i].tfn, list[i].seq);
                continue;
            }
            if (s->sio.stb_ftruncate(s, fd, list[i].wsz) < 0)
            {
                cfg_eprint1(MODULE_AOR,
                    "trunc_pop: ftruncate failed tfn=%s seq=%d wsz=%lu",
                    list[i].tfn, list[i].seq, (unsigned long)list[i].wsz);
                s->sio.stb_close(s, fd);
                continue;
            }
            s->sio.stb_close(s, fd);

            cfg_lprint1(MODULE_AOR,
                "trunc_pop: truncated tfn=%s seq=%d wsz=%lu",
                list[i].tfn, list[i].seq, (unsigned long)list[i].wsz);

            /* mark node as freed in queue (match by tfn) */
            pthr_lock(&tq.mutex);
            for (node = tq.head; node; node = node->next)
            {
                if (node->seq == list[i].seq)
                {
                    node->seq = -1;
                    break;
                }
            }
            pthr_unlock(&tq.mutex);
        }
    }

    if (list) free(list);
    cfg_lprint1(MODULE_AOR, "trunc_pop: thread exit");
    return NULL;
}

/* ---------------------------------------------------------- */
/* init (called from aor_backup_err_list_init)                */
/* ---------------------------------------------------------- */
int
aor_trunc_queue_init(AOR *ar)
{
    pthread_t      tid;
    pthread_attr_t attr;
    int            ret = -1;

    memset(&tq, 0, sizeof(tq));
    pthread_mutex_init(&tq.mutex, NULL);
    tq.ar = ar;

    pthread_attr_init(&attr);
    pthread_attr_setdetachstate(&attr, PTHREAD_CREATE_DETACHED);
    pthread_attr_setstacksize(&attr, AOR_THR_STACKSZ);

    if (pthread_create(&tid, &attr, _trunc_pop_loop, ar) != 0)
    {
        cfg_eprint1(MODULE_AOR, "trunc_queue_init: pthread_create failed errno=%d", errno);
        GO_ERROR(err_1)
    }

    cfg_lprint1(MODULE_AOR, "trunc_queue_init: thread started");
    ret = 0;

err_1:
    pthread_attr_destroy(&attr);
    return ret;
}
""".lstrip()

path = f"{BASE}/func/trunc_bthr.c"
with sftp.open(path, 'w') as f:
    f.write(trunc_content)
print(f"[OK] wrote {path}")

# ============================================================
# 2. Modify redo_backup.c — replace #if 1 block (lines 614-629)
# ============================================================
path_rb = f"{BASE}/func/redo_backup.c"
with sftp.open(path_rb, 'r') as f:
    lines = f.readlines()

# Find the #if 1 block around line 614
old_block = [
    '#if 1\n',
]

start_idx = None
end_idx = None
for i, line in enumerate(lines):
    if line.strip() == '#if 1' and i >= 610 and i <= 620:
        start_idx = i
    if start_idx is not None and line.strip() == '#endif' and i > start_idx:
        end_idx = i
        break

if start_idx is not None and end_idx is not None:
    # Get indentation from existing #if 1 line
    indent = ''
    for ch in lines[start_idx]:
        if ch in (' ', '\t'):
            indent += ch
        else:
            break

    new_block = [
        f'{indent}#if 1\n',
        f'{indent}  if (rd->ar->par.flg & AOR_FLG_BACKU_USE_REDO)\n',
        f'{indent}  {{\n',
        f'{indent}    aor_trunc_queue_push(rd->ar, rd->fseq, rd->tfn, rd->wsz);\n',
        f'{indent}  }}\n',
        f'{indent}#endif\n',
    ]
    lines[start_idx:end_idx+1] = new_block
    with sftp.open(path_rb, 'w') as f:
        f.writelines(lines)
    print(f"[OK] modified {path_rb} (lines {start_idx+1}-{end_idx+1} replaced)")
else:
    print(f"[WARN] could not find #if 1 block in {path_rb} around line 614")

# ============================================================
# 3. Modify aor_func.h — add declarations
# ============================================================
path_hdr = f"{BASE}/aor_func.h"
with sftp.open(path_hdr, 'r') as f:
    hdr_content = f.read()

decl_push = 'extern void     aor_trunc_queue_push(AOR *ar, int seq, const char *tfn, uint64_t wsz);'
decl_init = 'extern int      aor_trunc_queue_init(AOR *ar);'

if 'aor_trunc_queue_push' not in hdr_content:
    # Insert after aor_redo_queue_push declaration
    anchor = 'extern void     aor_redo_queue_push'
    idx = hdr_content.find(anchor)
    if idx >= 0:
        # Find end of that line
        eol = hdr_content.find('\n', idx)
        insert_pos = eol + 1
        insert_text = f'\n{decl_push}\n{decl_init}\n'
        hdr_content = hdr_content[:insert_pos] + insert_text + hdr_content[insert_pos:]
        with sftp.open(path_hdr, 'w') as f:
            f.write(hdr_content)
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
    mk_content = f.read()

if 'trunc_bthr.o' not in mk_content:
    anchor_mk = 'func/redo_bthr.o \\'
    idx_mk = mk_content.find(anchor_mk)
    if idx_mk >= 0:
        insert_after = idx_mk + len(anchor_mk)
        # Find the newline
        nl = mk_content.find('\n', insert_after)
        if nl < 0:
            nl = len(mk_content)
        # Get leading whitespace from the anchor line
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
            f.write(mk_content)
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
    rb2_lines = f.readlines()

# Find "is_init = 1;" in aor_backup_err_list_init
init_insert_done = False
for i, line in enumerate(rb2_lines):
    if 'is_init = 1;' in line and not init_insert_done:
        # Check if trunc_queue_init already present nearby
        nearby = ''.join(rb2_lines[max(0,i-5):i+5])
        if 'aor_trunc_queue_init' in nearby:
            print(f"[SKIP] trunc_queue_init already in {path_rb2}")
            init_insert_done = True
            break

        # Get indentation
        indent2 = ''
        for ch in line:
            if ch in (' ', '\t'):
                indent2 += ch
            else:
                break

        insert_lines = [
            f'\n',
            f'{indent2}if (ar->par.flg & AOR_FLG_BACKU_USE_REDO)\n',
            f'{indent2}{{\n',
            f'{indent2}    if (aor_trunc_queue_init(ar) < 0)\n',
            f'{indent2}        GO_ERROR(err_1)\n',
            f'{indent2}}}\n',
        ]
        # Insert BEFORE is_init = 1
        rb2_lines[i:i] = insert_lines
        with sftp.open(path_rb2, 'w') as f:
            f.writelines(rb2_lines)
        print(f"[OK] modified {path_rb2} (added trunc_queue_init before is_init=1)")
        init_insert_done = True
        break

if not init_insert_done:
    print(f"[WARN] could not find 'is_init = 1;' in {path_rb2}")

sftp.close()
ssh.close()
print("\n[DONE] All files written/modified successfully.")
