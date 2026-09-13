"""Linux root-only sandbox proof of M2 DAC permissions using disposable numeric UIDs.
No users are created; children drop all supplementary groups before testing synthetic files.
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path
import pytest

pytestmark=pytest.mark.skipif(sys.platform!='linux' or os.geteuid()!=0,
                              reason='Linux root required for isolated setuid permission fixture')


def test_real_uid_boundaries():
    root=Path(tempfile.mkdtemp(prefix='funding-m2-dac-',dir='/tmp'));root.chmod(0o755)
    roles={'core':(61001,[61010,61011,61012]),'interface':(61002,[61010,61011]),'collector':(61003,[61011,61012])}
    try:
        for role,(uid,groups) in roles.items():
            p=root/role;p.mkdir();os.chown(p,uid,uid);p.chmod(0o700)
            for name in ('state','secret'):
                q=p/name;q.write_text('synthetic only');os.chown(q,uid,uid);q.chmod(0o600)
        public=root/'public';public.mkdir();os.chown(public,61003,61011);public.chmod(0o2750)
        q=public/'table.json';q.write_text('{}');os.chown(q,61003,61011);q.chmod(0o640)
        shared=root/'shared';shared.mkdir();os.chown(shared,0,61012);shared.chmod(0o2770)
        q=shared/'okxdex.pace';q.write_text('0');os.chown(q,0,61012);q.chmod(0o660)
        for role,(uid,groups) in roles.items():
            pid=os.fork()
            if pid==0:
                try:
                    os.setgroups(groups);os.setgid(uid);os.setuid(uid)
                    assert (root/role/'secret').read_text()=='synthetic only'
                    for other in roles.keys()-{role}:
                        try:(root/other/'secret').read_text()
                        except PermissionError:pass
                        else:raise AssertionError('cross-role secret read')
                    assert (public/'table.json').read_text()=='{}'
                    if role!='collector':
                        try:(public/'table.json').write_text('bad')
                        except PermissionError:pass
                        else:raise AssertionError('public snapshot writable by reader')
                    try:
                        with open(shared/'okxdex.pace','r+') as f:f.write('1')
                    except PermissionError:
                        assert role=='interface'
                    else:assert role!='interface'
                    os._exit(0)
                except BaseException:
                    os._exit(1)
            _,status=os.waitpid(pid,0)
            assert os.waitstatus_to_exitcode(status)==0,role
    finally:shutil.rmtree(root)
