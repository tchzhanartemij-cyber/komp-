"""Isolated QA regression entry point; no provider calls or browser required."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.request
import urllib.error

ROOT = Path(__file__).resolve().parents[1]

class QA(unittest.TestCase):
    def test_install_includes_trusted_sibling(self):
        install = (ROOT/"install.sh").read_text()
        copy_line = next(line for line in install.splitlines() if line.startswith("cp "))
        self.assertIn('"$SRC_DIR/agent.py"', copy_line)
        self.assertIn('ExecStart=/usr/bin/python3 -I', install)

    def test_frontend_regressions(self):
        if not shutil.which('node'):
            self.skipTest('node unavailable')
        subprocess.run(['node', str(ROOT/'tests/qa_frontend.cjs')], check=True)

    def test_javascript_syntax(self):
        if not shutil.which('node'):
            self.skipTest('node unavailable')
        html = (ROOT/'index.html').read_text()
        script = html.split('<script>')[1].split('</script>')[0]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'dashboard.js'
            path.write_text(script)
            subprocess.run(['node', '--check', str(path)], check=True)

    def test_trusted_loader_registration_and_symlink(self):
        source = (ROOT/'server.py').read_text()
        loader = source[source.index('import importlib.util'):source.index('\nimport csv')]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sibling = root/'agent.py'
            sibling.write_text('from dataclasses import dataclass\n@dataclass\nclass Agent: value: int = 1\nclass Unavailable(Exception): pass\ndef queue_snapshot(): pass\n')
            script = "import sys\nbefore=list(sys.path)\n__file__=" + repr(str(root/'server.py')) + "\n" + loader + "\nassert sys.path == before\nassert Agent().value == 1\nassert sys.modules['agent'].Agent is Agent\n"
            subprocess.run(['python3','-I','-c',script],cwd=root,check=True,capture_output=True)
            outside = root/'outside'; outside.mkdir()
            (outside/'agent.py').write_text('raise AssertionError("outside trusted directory")')
            sibling.unlink(); sibling.symlink_to(outside/'agent.py')
            result = subprocess.run(['python3','-I','-c',script],cwd=root,capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('trusted dashboard sibling', result.stderr)
            self.assertNotIn('AssertionError', result.stderr)

    def test_exact_isolated_startup(self):
        # Execute the actual absolute script under -I even in socket-denying sandboxes.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'home').mkdir()
            token = root/'token'; token.write_text('synthetic-disposable-qa-token'); token.chmod(0o600)
            try:
                with socket.socket() as sock:
                    sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
            except PermissionError:
                port = 18793
            env = {'HOME':str(root/'home'), 'LIFE_DIR':str(root/'life'), 'PATH':'/usr/bin:/bin',
                   'DASHBOARD_HOST':'127.0.0.1', 'DASHBOARD_PORT':str(port),
                   'DASHBOARD_TOKEN_FILE':str(token), 'SVODKA_AGENT_ENABLED':'0'}
            # A hostile cwd/PYTHONPATH must never supply the sibling import.
            (root/'agent.py').write_text('raise RuntimeError("untrusted cwd module")')
            env['PYTHONPATH'] = str(root)
            proc = subprocess.Popen(['python3','-I',str(ROOT/'server.py')],cwd=root,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            try:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                base = f'http://127.0.0.1:{port}'
                for _ in range(100):
                    if proc.poll() is not None:
                        out, err = proc.communicate()
                        if 'PermissionError: [Errno 1]' in err and 'socket' in err:
                            self.assertNotIn('ModuleNotFoundError',err)
                            self.skipTest('Exact -I imports succeeded; sandbox prohibits server socket')
                        self.fail(out+err)
                    try:
                        opener.open(base+'/api/data',timeout=.2)
                    except urllib.error.HTTPError as exc:
                        self.assertEqual(exc.code,401)
                        break
                    except (OSError, urllib.error.URLError):
                        time.sleep(.02)
                else:
                    self.fail('server did not become ready')
                auth = opener.open(base+'/?k=synthetic-disposable-qa-token',timeout=2)
                cookie = auth.headers['Set-Cookie'].split(';')[0]
                self.assertIn('Сводка',auth.read().decode())
                for endpoint in ('/api/data','/api/agent','/api/office'):
                    with opener.open(urllib.request.Request(base+endpoint,headers={'Cookie':cookie}),timeout=2) as response:
                        payload = json.load(response)
                    if endpoint=='/api/agent': self.assertFalse(payload['available'])
            finally:
                if proc.poll() is None: proc.terminate()
                proc.communicate(timeout=5)

if __name__ == '__main__':
    unittest.main()
