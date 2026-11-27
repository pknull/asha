"""Pi terminal contract: real argv, rendering, and native binding boundaries."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from lib.control.harness import validate_harness
from lib.control.rooms import room_launch_argv
from lib.control.session_harness import CAPABILITIES
from lib.control.session_hub import native_binding, same_conversation
from lib.control.session_selection import normalize
from lib.control.store import StoreError

ROOT = Path(__file__).resolve().parents[2]


class PiControlTests(unittest.TestCase):
    def test_native_selection_and_resume(self):
        self.assertEqual(validate_harness('pi'), 'pi')
        selection = normalize('pi', 'terminal', 'ollama/model:12b', 'low')
        self.assertEqual(room_launch_argv(ROOT, 'pi', '-literal prompt', selection=selection, resume_id='native-id'),
                         [str(ROOT/'bin/asha'), 'pi', '--model', 'ollama/model:12b', '--thinking', 'low', '--session', 'native-id', '--', '-literal prompt'])
        self.assertFalse(CAPABILITIES['pi']['managed'])
        with self.assertRaises(StoreError): normalize('pi', 'terminal', effort='unknown')

    def test_only_native_session_switch_can_rebind(self):
        row = {'harness':'pi', 'generation':1, 'project':str(ROOT), 'native_binding':{'generation':1,'native_id':'old'}}
        with self.assertRaises(StoreError): native_binding(row, 'session-start', 'nested', str(ROOT), 'startup')
        for source in ('pinew', 'piresume', 'pifork'):
            self.assertEqual(native_binding(row, 'session-start', 'new', str(ROOT), source)['native_id'], 'new')
        row['harness'] = 'codex'
        with self.assertRaises(StoreError): native_binding(row, 'session-start', 'new', str(ROOT), 'pinew')

    def test_attachment_prefix_is_transported_as_literal_message(self):
        prompt='@README.md explain this file\nMore instructions'
        argv=room_launch_argv(ROOT,'pi',prompt)
        self.assertEqual(argv[-1],'\n'+prompt)
        self.assertFalse(argv[-1].startswith('@'))

    def test_actor_commands_reject_inherited_other_conversation(self):
        row={'harness':'pi','generation':1,'native_binding':{'generation':1,'native_id':'current'}}
        same_conversation(row,'current')
        with self.assertRaises(StoreError): same_conversation(row,'nested')


class PiInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.native = self.home/'pi-cli'
        self.native.write_text('#!/usr/bin/env python3\nimport json,sys\nprint("1.6.1" if sys.argv[1:]==["--version"] else json.dumps(sys.argv[1:]))\n')
        self.native.chmod(0o755)
        self.env = {'HOME':str(self.home), 'PATH':os.environ['PATH'], 'ASHA_PI_CMD':str(self.native), 'LANG':'C.UTF-8'}
        self.pi = self.home/'.pi/agent'

    def run_cli(self, *args, ok=True, env=None):
        result = subprocess.run([str(ROOT/'bin/asha'), *args], env=env or self.env, cwd=ROOT,
                                capture_output=True, text=True, timeout=90)
        if ok: self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def install(self):
        return self.run_cli('install','pi','--only','session')

    def test_install_reinstall_uninstall_preserves_user_files(self):
        self.install()
        extension = self.pi/'extensions/asha.ts'
        before = extension.read_bytes()
        self.assertIn(str(ROOT).encode(), before)
        prompt = (self.pi/'prompts/session-save.md').read_text()
        self.assertIn('single-agent', prompt)
        self.assertNotIn('allowed-tools:', prompt)
        self.assertFalse((self.pi/'agents').exists())
        self.install()
        self.assertEqual(extension.read_bytes(), before)
        settings = self.pi/'settings.json'; settings.write_text('{"user":true}')
        session = self.pi/'sessions/keep.jsonl'; session.parent.mkdir(); session.write_text('private')
        self.run_cli('uninstall','pi')
        self.assertFalse(extension.exists())
        self.assertEqual(settings.read_text(), '{"user":true}')
        self.assertEqual(session.read_text(), 'private')

    def test_foreign_and_modified_artifacts_refuse_overwrite(self):
        target = self.pi/'extensions/asha.ts'; target.parent.mkdir(parents=True); target.write_text('foreign')
        self.assertNotEqual(self.run_cli('install','pi','--only','session',ok=False).returncode, 0)
        self.assertEqual(target.read_text(), 'foreign')
        target.unlink(); self.install(); target.write_text('modified')
        self.assertNotEqual(self.run_cli('install','pi','--only','session',ok=False).returncode, 0)
        self.assertEqual(target.read_text(), 'modified')

    def test_dry_run_and_profile_arguments(self):
        self.run_cli('install','pi','--only','session','--dry-run')
        self.assertFalse(self.pi.exists())
        self.install()
        identity = self.home/'.asha'
        for name in ('soul','voice','keeper','operation'):
            (identity/(name+'.md')).write_text(name.upper()+' FIXTURE\n')
        for profile in ('worker','room','chair'):
            result=self.run_cli('pi','payload with spaces',env=dict(self.env,ASHA_SESSION_PROFILE=profile))
            args=json.loads(result.stdout)
            self.assertEqual(args[-1],'payload with spaces')
            self.assertEqual('--append-system-prompt' in args,profile!='worker')

    def test_native_directory_override(self):
        other=self.home/'alternate'
        self.run_cli('install','pi','--only','session',env=dict(self.env,PI_CODING_AGENT_DIR=str(other)))
        self.assertTrue((other/'extensions/asha.ts').is_file())
        self.assertFalse(self.pi.exists())

    def test_real_policy_adapter_blocks_secret_path(self):
        result=subprocess.run([str(ROOT/'plugins/session/hooks/handlers/pi-policy-adapter.sh')],env=self.env,
            input=json.dumps({'tool_name':'read','tool_input':{'path':str(self.home/'.env')},'cwd':str(self.home),'session_id':'fixture'}),
            text=True,capture_output=True,timeout=10)
        self.assertEqual(result.returncode,2,result.stderr)
        self.assertIn('Secrets file access denied',result.stderr)

    def test_doctor_distinguishes_native_only_and_lost_manifest(self):
        native_skill=self.pi/'skills/user/SKILL.md'
        native_skill.parent.mkdir(parents=True)
        native_skill.write_text('---\nname: user\ndescription: User fixture\n---\nUser fixture\n')
        result=self.run_cli('doctor','pi',ok=False)
        self.assertIn('pi not configured by Asha',result.stdout)
        self.run_cli('install','pi')
        (self.home/'.asha/install-manifests/pi.json').unlink()
        result=self.run_cli('doctor','pi',ok=False)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('Pi artifact drift',result.stdout)
        self.assertNotIn('pi not configured by Asha',result.stdout)


if __name__ == '__main__': unittest.main()
