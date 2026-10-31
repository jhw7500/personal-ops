"""Browser-only fixtures; no host authority, external requests or Git effects."""
if __name__ == '__main__':
    import json
    from pathlib import Path
    import unittest
    from playwright.sync_api import sync_playwright, expect

    HTML = (Path(__file__).resolve().parents[1] / 'git-commit-access-approval.html').read_text()
    CAP = '9' * 64
    GOOD = {'component': 'git_commit_access', 'project': 'repo', 'card_id': 'a' * 32,
            'state': 'pending', 'repository': '/tmp/disposable-repository',
            'branch': 'feature/test', 'head': 'b' * 40, 'operations': ['commit'],
            'duration_seconds': 86400, 'expires_at': '2099-01-01T00:00:00Z',
            'executes_on_approval': False}

    class BrowserTest(unittest.TestCase):
        @classmethod
        def setUpClass(cls):
            cls.pw = sync_playwright().start()
            cls.browser = cls.pw.chromium.launch(headless=True)

        @classmethod
        def tearDownClass(cls):
            cls.browser.close()
            cls.pw.stop()

        def setUp(self):
            self.context = self.browser.new_context(viewport={'width': 390, 'height': 950})
            self.addCleanup(self.context.close)
            self.page = self.context.new_page()
            self.errors = []
            self.page.on('pageerror', lambda error: self.errors.append(str(error)))

        def load(self, patch=None, cap=CAP, fail=False, mismatch=False, reply_state=None, status_reply=None):
            card = {**GOOD, **(patch or {})}
            self.page.set_content('''<!doctype html><html><style>
            body{margin:0}iframe{width:100%;height:1200px;border:0}
            </style><iframe></iframe><script>
            window.contexts=[];
            window.addEventListener('message',e=>{
              if(e.source!==document.querySelector('iframe').contentWindow)return;
              const d=e.data;
              if(d?.method==='ui/update-model-context'){
                contexts.push(d.params);
                e.source.postMessage({jsonrpc:'2.0',id:d.id,result:{}},'*');
              }
            });</script></html>''')
            self.frame = self.page.frames[1]
            self.frame.evaluate('''({card,cap,fail,mismatch,replyState,statusReply})=>{
              window.calls=[];
              window.openai={toolOutput:card,
                toolResponseMetadata:cap?{git_commit_access_confirmation:cap}:{},
                callTool:async(name,args)=>{
                  calls.push({name,args});
                  if(name==='get_git_commit_access')return {structuredContent:statusReply||{project:card.project,active:false,reason:'MISSING_GRANT'}};
                  if(fail)throw Error('fixture ambiguity');
                  let next={...card};
                  if(name==='confirm_git_commit_access'){
                    next.state='active';next.grant_id='c'.repeat(32);
                    next.grant_expires_at='2099-01-01T00:00:00Z';
                  }else if(name==='cancel_git_commit_access')next.state='cancelled';
                  else if(name==='revoke_git_commit_access')next.state='revoked';
                  else throw Error('unexpected tool');
                  if(mismatch)next.card_id='d'.repeat(32);
                  if(replyState){
                    next.state=replyState;
                    if(replyState==='active'){
                      next.grant_id='c'.repeat(32);
                      next.grant_expires_at='2099-01-01T00:00:00Z';
                    }
                  }
                  return {structuredContent:next};
                }};
            }''', {'card': card, 'cap': cap, 'fail': fail, 'mismatch': mismatch, 'replyState': reply_state, 'statusReply': status_reply})
            self.frame.set_content(HTML)
            return self.frame

        def calls(self):
            return self.frame.evaluate('window.calls')

        def test_no_automatic_approval_and_explicit_consent(self):
            f = self.load()
            expect(f.locator('#approve')).to_be_disabled()
            self.assertEqual(self.calls(), [])
            f.locator('#consent').check()
            expect(f.locator('#approve')).to_be_enabled()
            f.locator('#approve').click()
            expect(f.locator('#state')).to_have_text('권한 활성')
            self.assertEqual(len(self.calls()), 1)
            self.assertEqual(self.calls()[0]['name'], 'confirm_git_commit_access')
            self.assertEqual(self.calls()[0]['args']['confirmation_token'], CAP)
            expect(f.locator('#revoke')).to_be_disabled()
            self.assertNotIn(CAP, json.dumps(self.page.evaluate('window.contexts')))
            self.assertNotIn(CAP, f.locator('body').inner_text())
            self.assertEqual(self.errors, [])

        def test_explicit_push_card_displays_origin_budget_and_requires_consent(self):
            f=self.load({'permission_version':2,'operations':['commit','push'],
                         'origin':'https://github.com/jhw7500/tossApp.git','push_limit':3})
            expect(f.locator('#origin')).to_contain_text('github.com/jhw7500/tossApp')
            expect(f.locator('#pushBudget')).to_contain_text('3회')
            expect(f.locator('#approve')).to_be_disabled()
            self.assertEqual(self.calls(),[])
            f.locator('#consent').check();f.locator('#approve').click()
            expect(f.locator('#state')).to_have_text('권한 활성')
            self.assertEqual([x['name'] for x in self.calls()],['confirm_git_commit_access'])

        def test_runtime_reapproval_card_enables_fresh_explicit_consent(self):
            f=self.load({'renewal_reason':'RUNTIME_CHANGED','replaces_grant_id':'d'*32})
            expect(f.locator('#approve')).to_be_visible()
            expect(f.locator('#approve')).to_be_disabled()
            f.locator('#consent').check()
            expect(f.locator('#approve')).to_be_enabled()

        def test_response_loss_resolves_with_read_only_status_without_second_approval(self):
            status={'project':'repo','active':True,'repository':GOOD['repository'],
                    'branch':GOOD['branch'],'operations':['commit'],'duration_seconds':86400,
                    'grant_id':'c'*32,'expires_at':'2099-01-01T00:00:00Z'}
            f=self.load(fail=True,status_reply=status)
            f.locator('#consent').check();f.locator('#approve').click()
            expect(f.locator('#status')).to_contain_text('결과를 확정할 수 없습니다')
            f.locator('#refresh').click()
            expect(f.locator('#state')).to_have_text('권한 활성')
            expect(f.locator('#status')).to_contain_text('다시 승인할 필요가 없습니다')
            self.assertEqual([x['name'] for x in self.calls()],['confirm_git_commit_access','get_git_commit_access'])
            self.assertNotIn(CAP,json.dumps(self.page.evaluate('window.contexts')))
            self.assertEqual(self.errors,[])

        def test_status_for_another_repository_never_claims_recovered_approval(self):
            status={'project':'repo','active':True,'repository':'/tmp/another',
                    'branch':GOOD['branch'],'operations':['commit'],'duration_seconds':86400,
                    'grant_id':'c'*32,'expires_at':'2099-01-01T00:00:00Z'}
            f=self.load(fail=True,status_reply=status)
            f.locator('#consent').check();f.locator('#approve').click();f.locator('#refresh').click()
            expect(f.locator('#status')).to_contain_text('현재 상태를 확인하지 못했습니다')
            expect(f.locator('#state')).not_to_have_text('권한 활성')
            self.assertEqual(self.page.evaluate('window.contexts'),[])

        def test_inactive_refresh_updates_display_and_model_context(self):
            variants = [
                ({}, 'EXPIRED'),
                ({'permission_version': 2, 'operations': ['commit', 'push'],
                  'origin': 'https://github.com/jhw7500/tossApp.git', 'push_limit': 3},
                 'REVOKED'),
            ]
            for patch, reason in variants:
                with self.subTest(reason=reason):
                    f = self.load(patch, status_reply={
                        'project': 'repo', 'active': False, 'reason': reason,
                    })
                    f.locator('#consent').check()
                    f.locator('#approve').click()
                    expect(f.locator('#state')).to_have_text('권한 활성')
                    f.locator('#refresh').click()
                    expect(f.locator('#state')).to_contain_text('현재 사용 불가')
                    self.page.wait_for_function('window.contexts.length >= 2')
                    latest = self.page.evaluate('window.contexts.at(-1)')
                    access = latest['structuredContent']['gitCommitAccess']
                    self.assertIs(access['usable_for_current_context'], False)
                    self.assertEqual(access['reason'], reason)
                    self.assertIsNone(access['pushes_remaining'])
                    self.assertIn('현재 사용 불가', latest['content'][0]['text'])
                    self.assertEqual([c['name'] for c in self.calls()],
                                     ['confirm_git_commit_access', 'get_git_commit_access'])
                    expect(f.locator('#approve')).to_be_disabled()
                    expect(f.locator('#revoke')).to_be_disabled()
                    self.assertEqual(self.errors, [])

        def test_inactive_refresh_locks_an_existing_active_card(self):
            f = self.load(
                {'state': 'active', 'grant_id': 'c' * 32,
                 'grant_expires_at': '2099-01-01T00:00:00Z'},
                status_reply={'project': 'repo', 'active': False, 'reason': 'REVOKED'},
            )
            expect(f.locator('#revoke')).to_be_enabled()
            f.locator('#refresh').click()
            expect(f.locator('#state')).to_contain_text('현재 사용 불가')
            expect(f.locator('#revoke')).to_be_disabled()
            self.assertNotIn('철회 가능', f.locator('#state').inner_text())
            self.assertEqual([c['name'] for c in self.calls()], ['get_git_commit_access'])

        def test_missing_grant_refresh_preserves_fresh_pending_consent(self):
            f = self.load(status_reply={
                'project': 'repo', 'active': False, 'reason': 'MISSING_GRANT',
            })
            f.locator('#refresh').click()
            expect(f.locator('#status')).to_contain_text('현재 사용할 수')
            expect(f.locator('#state')).to_have_text('승인 대기')
            f.locator('#consent').check()
            expect(f.locator('#approve')).to_be_enabled()
            self.assertEqual([c['name'] for c in self.calls()], ['get_git_commit_access'])

        def test_cancel_does_not_require_consent_or_grant(self):
            f = self.load()
            f.locator('#cancel').click()
            expect(f.locator('#state')).to_have_text('승인 취소')
            self.assertEqual([c['name'] for c in self.calls()], ['cancel_git_commit_access'])

        def test_active_card_can_only_revoke(self):
            f = self.load({'state': 'active', 'grant_id': 'c' * 32,
                           'grant_expires_at': '2099-01-01T00:00:00Z'})
            expect(f.locator('#approve')).to_be_hidden()
            f.locator('#revoke').click()
            expect(f.locator('#state')).to_have_text('권한 철회')
            self.assertEqual([c['name'] for c in self.calls()], ['revoke_git_commit_access'])

        def test_unusable_grant_is_revocable_without_claiming_current_access(self):
            f = self.load({'state': 'active', 'grant_id': 'c' * 32,
                           'grant_expires_at': '2099-01-01T00:00:00Z',
                           'usable_for_current_context': False})
            expect(f.locator('#state')).to_have_text('현재 사용 불가 · 철회 가능')
            expect(f.locator('#approve')).to_be_hidden()
            expect(f.locator('#revoke')).to_be_enabled()
            f.locator('#revoke').click()
            expect(f.locator('#state')).to_have_text('권한 철회')
            self.assertEqual([c['name'] for c in self.calls()], ['revoke_git_commit_access'])

        def test_missing_private_capability_disables_actions(self):
            f = self.load(cap=None)
            expect(f.locator('#approve')).to_be_disabled()
            expect(f.locator('#cancel')).to_be_disabled()
            self.assertEqual(self.calls(), [])

        def test_invalid_scope_never_enables(self):
            for patch in [{'operations': ['push']}, {'operations': ['commit', 'push']},
                          {'branch': 'main'}, {'duration_seconds': 86401}, {'repository': '../repo'}]:
                f = self.load(patch)
                expect(f.locator('#approve')).to_be_disabled()
                self.assertEqual(self.calls(), [])

        def test_expired_card_cannot_be_used(self):
            f = self.load({'expires_at': '2000-01-01T00:00:00Z'})
            expect(f.locator('#approve')).to_be_disabled()
            expect(f.locator('#cancel')).to_be_disabled()

        def test_programmatic_click_cannot_approve(self):
            f = self.load()
            f.locator('#consent').check()
            f.evaluate('document.getElementById("approve").click()')
            self.assertEqual(self.calls(), [])

        def test_ambiguous_transport_locks_without_retry(self):
            f = self.load(fail=True)
            f.locator('#consent').check()
            f.locator('#approve').click()
            expect(f.locator('#status')).to_contain_text('결과를 확정할 수 없습니다')
            expect(f.locator('#approve')).to_be_disabled()
            self.assertEqual(len(self.calls()), 1)

        def test_mismatched_response_never_claims_approval(self):
            f = self.load(mismatch=True)
            f.locator('#consent').check()
            f.locator('#approve').click()
            expect(f.locator('#status')).to_contain_text('결과를 확정할 수 없습니다')
            self.assertEqual(self.page.evaluate('window.contexts'), [])

        def test_wrong_action_result_locks_without_claiming_success(self):
            active = {'state': 'active', 'grant_id': 'c' * 32,
                      'grant_expires_at': '2099-01-01T00:00:00Z'}
            for action, initial, reply in [('approve', None, 'pending'),
                                           ('cancel', None, 'active'),
                                           ('revoke', active, 'pending')]:
                with self.subTest(action=action, reply=reply):
                    f = self.load(initial, reply_state=reply)
                    if action == 'approve':
                        f.locator('#consent').check()
                    f.locator('#' + action).click()
                    expect(f.locator('#status')).to_contain_text('결과를 확정할 수 없습니다')
                    expect(f.locator('#' + action)).to_be_disabled()
                    self.assertEqual(len(self.calls()), 1)
                    self.assertEqual(self.page.evaluate('window.contexts'), [])
                    self.assertEqual(self.errors, [])

        def test_nonparent_message_is_ignored(self):
            f = self.load()
            f.evaluate('''()=>window.dispatchEvent(new MessageEvent('message',{
              source:window,data:{jsonrpc:'2.0',method:'ui/notifications/tool-result',
              params:{structuredContent:{...window.openai.toolOutput,state:'cancelled'}}}}))''')
            expect(f.locator('#state')).to_have_text('승인 대기')
            self.assertEqual(self.calls(), [])

        def test_without_native_bridge_controls_stay_locked(self):
            self.page.set_content('<iframe></iframe>')
            self.frame=self.page.frames[1]
            self.frame.set_content(HTML)
            expect(self.frame.locator('#approve')).to_be_disabled()
            expect(self.frame.locator('#status')).to_contain_text('지원되는 ChatGPT')

        def test_scope_is_text_and_mobile_widths_fit(self):
            f = self.load({'repository': '/tmp/<img src=x onerror=alert(1)>'})
            self.assertEqual(f.locator('img').count(), 0)
            for width in [320, 390, 640]:
                self.page.set_viewport_size({'width': width, 'height': 1000})
                self.assertLessEqual(f.evaluate('document.documentElement.scrollWidth'),
                                     f.evaluate('innerWidth'))
            self.assertEqual(self.errors, [])

    unittest.main()
