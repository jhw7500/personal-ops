"""Resource and owner-callback contracts; all effects confined to fixtures."""
import unittest
from tests import test_git_commit_access_backend_router as backend

class GitCommitAccessRouterTest(backend.GitCommitAccessBackendRouterTest):
    async def test_owner_callback_reuses_grant_through_real_stdio_router(self):
        async with backend.fixture_client() as (client,fx,dirs):
            specs={t.name:t for t in (await client.list_tools()).tools}
            uri='ui://git-commit-access/approval-v1.html'
            self.assertEqual(specs['open_git_commit_access_approval'].meta['ui']['resourceUri'],uri)
            resources=await client.list_resources()
            self.assertIn(uri,{str(r.uri) for r in resources.resources})
            ui=await client.read_resource(uri)
            self.assertEqual(ui.contents[0].mimeType,'text/html;profile=mcp-app')
            self.assertEqual(ui.contents[0].meta['ui']['csp'],{'connectDomains':[],'resourceDomains':[]})
            self.assertIn('표시된 범위 승인',ui.contents[0].text)
            self.assertIn('executes_on_approval',ui.contents[0].text)
        await self.test_two_commits_then_revocation_blocks_third()

if __name__=='__main__':unittest.main()
