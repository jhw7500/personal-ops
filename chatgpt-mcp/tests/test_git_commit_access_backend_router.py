"""Backend-only MCP protocol tests; not approval-UI or deployment verification.
All repositories, state and owner callbacks below are disposable fixtures.
The blocked UI artifact is neither created nor replaced by this test.
"""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from test_extended_chat_approval_router import LAUNCHER, HERE, TESTS, text_reply, write_registered_fixture, run_git

@asynccontextmanager
async def fixture_client():
    with tempfile.TemporaryDirectory(prefix='git24-backend-protocol-') as folder:
        root = Path(folder)
        fx = write_registered_fixture(root)
        run_git(fx['root'], 'checkout', '-b', 'feature/commit-access')
        run_git(fx['root'], 'config', '--local', 'user.name', 'Git Access Fixture')
        run_git(fx['root'], 'config', '--local', 'user.email', 'fixture@example.invalid')
        dirs = {key: root / name for key, name in (
            ('HOME','home'), ('XDG_STATE_HOME','state'),
            ('XDG_CONFIG_HOME','config-home'), ('XDG_CACHE_HOME','cache'))}
        for d in dirs.values():
            d.mkdir(mode=0o700)
        # Reuse installed interpreter binaries only; no host config, state or credentials.
        dep = dirs['HOME'] / '.local/share/uv/tools/serena-agent'
        dep.parent.mkdir(parents=True)
        dep.symlink_to(Path(sys.executable).parent.parent, target_is_directory=True)
        launcher = root / 'launcher.py'
        # Pin only the interpreter dependency; HOME/config/state remain private fixtures.
        fixture_launcher = LAUNCHER.replace('import host_access,server\n', 'import host_access,server\nserver.SERENA_ENV=Path(sys.executable).parent.parent\n')
        launcher.write_text(fixture_launcher)
        config = root / 'launcher-config.json'
        config.write_text(json.dumps({'module_dir':str(HERE), 'tests_dir':str(TESTS),
            'host_config':str(fx['config']), 'projects':str(fx['projects_file'])}))
        env = {key:str(value) for key,value in dirs.items()}
        env.update(PATH=os.environ.get('PATH','/usr/bin:/bin'), LANG='C.UTF-8',
                   PYTHONDONTWRITEBYTECODE='1', UV_OFFLINE='1')
        params = StdioServerParameters(command=sys.executable,
            args=['-B',str(launcher),str(config)],env=env)
        logpath = root / 'stderr.log'
        try:
            with logpath.open('w+') as log:
                async with asyncio.timeout(90):
                    async with stdio_client(params,errlog=log) as (read,write):
                        async with ClientSession(read,write) as client:
                            await client.initialize()
                            yield client,fx,dirs
        except BaseException:
            if logpath.exists():
                sys.stderr.write(logpath.read_text()[-8192:])
            raise

class GitCommitAccessBackendRouterTest(unittest.IsolatedAsyncioTestCase):
    async def ok(self,client,name,args):
        reply = await client.call_tool(name,args)
        self.assertFalse(reply.isError,text_reply(reply))
        return reply

    async def open_card(self,client):
        reply=await self.ok(client,'open_git_commit_access_approval',{'project':'tossApp','duration_seconds':86400})
        return reply,{'project':'tossApp','card_id':reply.structuredContent['card_id'],
            'confirmation_token':reply.meta['git_commit_access_confirmation']}

    async def plan(self,client,fx,value):
        (fx['root']/'server/src/main.ts').write_text(f'export default {value};\n')
        return await self.ok(client,'prepare_git_operation',{'project':'tossApp','action':'commit',
            'paths':['server/src/main.ts'],'message':'Fixture\n\n### References\n- Issue: https://github.com/jhw7500/personal-ops/issues/1'})

    async def test_backend_tools_registered_with_private_callbacks(self):
        async with fixture_client() as (client,fx,dirs):
            tools={tool.name:tool for tool in (await client.list_tools()).tools}
            self.assertIn('open_git_commit_access_approval',tools)
            self.assertIn('get_git_commit_access',tools)
            for name in ['confirm_git_commit_access','cancel_git_commit_access','revoke_git_commit_access']:
                self.assertEqual(tools[name].meta['ui']['visibility'],['app'])
                self.assertFalse(tools[name].inputSchema['additionalProperties'])
            state=await self.ok(client,'get_git_commit_access',{'project':'tossApp'})
            self.assertFalse(state.structuredContent['active'])

    async def test_open_and_forged_confirmation_never_authorize(self):
        async with fixture_client() as (client,fx,dirs):
            before=run_git(fx['root'],'rev-parse','HEAD').stdout.strip()
            opened,args=await self.open_card(client)
            cap=args['confirmation_token']
            self.assertNotIn(cap,text_reply(opened))
            self.assertNotIn(cap,json.dumps(opened.structuredContent))
            invalid=await client.call_tool('confirm_git_commit_access',{**args,'extra':cap})
            self.assertTrue(invalid.isError)
            self.assertEqual(text_reply(invalid),'INVALID_GIT_COMMIT_ACCESS_ARGUMENTS')
            self.assertNotIn(cap,text_reply(invalid))
            denied=await client.call_tool('confirm_git_commit_access',{**args,'confirmation_token':'b'*64})
            self.assertTrue(denied.isError)
            state=await self.ok(client,'get_git_commit_access',{'project':'tossApp'})
            self.assertFalse(state.structuredContent['active'])
            plan=await self.plan(client,fx,2)
            denied=await client.call_tool('execute_git_operation',{'project':'tossApp','operation_id':plan.structuredContent['operation_id']})
            self.assertTrue(denied.isError)
            self.assertEqual(before,run_git(fx['root'],'rev-parse','HEAD').stdout.strip())

    async def test_two_commits_then_revocation_blocks_third(self):
        async with fixture_client() as (client,fx,dirs):
            before=run_git(fx['root'],'rev-parse','HEAD').stdout.strip()
            opened,args=await self.open_card(client)
            granted=await self.ok(client,'confirm_git_commit_access',args)
            self.assertEqual(granted.structuredContent['state'],'active')
            self.assertEqual(before,run_git(fx['root'],'rev-parse','HEAD').stdout.strip())
            expiry=granted.structuredContent['grant_expires_at']
            for value in (2,3):
                plan=await self.plan(client,fx,value)
                self.assertFalse(plan.structuredContent['approval_required'])
                self.assertNotIn('approval_command',plan.structuredContent)
                done=await self.ok(client,'execute_git_operation',{'project':'tossApp','operation_id':plan.structuredContent['operation_id']})
                self.assertEqual(done.structuredContent['status'],'completed')
                self.assertEqual(done.structuredContent['authorization']['kind'],'git_commit_access')
                state=await self.ok(client,'get_git_commit_access',{'project':'tossApp'})
                self.assertEqual(state.structuredContent['expires_at'],expiry)
            self.assertEqual(run_git(fx['root'],'show','HEAD:server/src/main.ts').stdout,'export default 3;\n')
            self.assertEqual(run_git(fx['root'],'diff','--cached','--name-only').stdout,'')
            active,args=await self.open_card(client)
            self.assertEqual(active.structuredContent['grant_expires_at'],expiry)
            revoked=await self.ok(client,'revoke_git_commit_access',args)
            self.assertEqual(revoked.structuredContent['state'],'revoked')
            before_third=run_git(fx['root'],'rev-parse','HEAD').stdout.strip()
            plan=await self.plan(client,fx,4)
            public=plan.structuredContent
            self.assertEqual(public['component'],'git_operation')
            self.assertEqual(public['state'],'pending')
            self.assertTrue(public['approval_required'])
            self.assertFalse(public['authorization_verified'])
            self.assertFalse(public['receipt_verified'])
            self.assertFalse(public['executes_on_approval'])
            self.assertNotIn('approval_command',public)
            self.assertNotIn('approval_command',public['plan'])
            capability=plan.meta['git_operation_confirmation']
            self.assertNotIn(capability,text_reply(plan))
            self.assertNotIn(capability,json.dumps(public))
            self.assertEqual(before_third,run_git(fx['root'],'rev-parse','HEAD').stdout.strip())
            self.assertFalse((await self.ok(
                client,'get_git_commit_access',{'project':'tossApp'}
            )).structuredContent['active'])
            denied=await client.call_tool('execute_git_operation',{'project':'tossApp','operation_id':plan.structuredContent['operation_id']})
            self.assertTrue(denied.isError)
            self.assertEqual(before_third,run_git(fx['root'],'rev-parse','HEAD').stdout.strip())
            self.assertFalse((await self.ok(client,'get_git_commit_access',{'project':'tossApp'})).structuredContent['active'])

if __name__ == '__main__': unittest.main()
