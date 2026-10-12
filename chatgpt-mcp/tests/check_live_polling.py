"""Focused browser checks for the admin dashboard's live polling lanes."""

import sys
import unittest
from pathlib import Path

from playwright.sync_api import expect

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_admin_ui  # noqa: E402


class LivePollingBrowserTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        check_admin_ui.AdminBrowserTest.setUpClass()

    @classmethod
    def tearDownClass(cls):
        check_admin_ui.AdminBrowserTest.tearDownClass()

    def setUp(self):
        self.fixture = check_admin_ui.AdminBrowserTest(methodName='runTest')
        self.fixture.setUp()
        self.page = self.fixture.page
        self.store = self.fixture.store
        expect(self.page.locator('#updated')).to_contain_text('갱신')
        expect(self.page.locator('#log')).to_contain_text('start')
        self.page.locator('#follow').click()
        expect(self.page.locator('#follow')).to_have_text('자동 갱신 재개')

    def tearDown(self):
        self.fixture.tearDown()

    @staticmethod
    def _release(route):
        route.fulfill(response=route.fetch())

    def test_map_and_fast_panels_update_while_detail_responses_are_held(self):
        held = []
        for endpoint in ('progress', 'changes', 'log'):
            self.page.route(f'**/api/{endpoint}?*', lambda route: held.append(route))

        self.store.record_activity('alpha', 'read_file', 'started', 'live-fast-path')
        self.page.locator('#refresh').click()

        actor = self.page.locator(
            '#work-map .wd-actor[data-actor-id="entity:personal-ops-tunnel:coding-mcp"]'
        )
        expect(actor).to_have_attribute('data-room', 'analysis')
        expect(self.page.locator('#activity')).to_contain_text('시작')
        self.page.wait_for_timeout(50)
        self.assertGreaterEqual(len(held), 2)

        for route in held:
            self._release(route)

    def test_fast_poll_never_overlaps_itself(self):
        held_jobs = []
        self.page.route('**/api/jobs?project=alpha', lambda route: held_jobs.append(route))

        self.page.locator('#follow').click()
        expect(self.page.locator('#follow')).to_have_text('자동 갱신 일시정지')
        self.page.wait_for_timeout(250)
        self.assertEqual(len(held_jobs), 1)
        self.page.wait_for_timeout(1400)
        self.assertEqual(len(held_jobs), 1)

        self._release(held_jobs.pop(0))
        self.page.wait_for_timeout(1250)
        self.assertEqual(len(held_jobs), 1)
        self.page.locator('#follow').click()
        self._release(held_jobs.pop(0))

    def test_hidden_and_pause_stop_automatic_polling_until_visible_resume(self):
        counts = {'jobs': 0}

        def count_and_continue(kind, route):
            counts[kind] += 1
            route.continue_()

        self.page.route('**/api/jobs?*', lambda route: count_and_continue('jobs', route))

        self.page.wait_for_timeout(100)
        paused_jobs = counts['jobs']
        self.page.wait_for_timeout(1200)
        self.assertEqual(counts['jobs'], paused_jobs)
        self.page.evaluate("""() => {
            Object.defineProperty(document, 'hidden', {configurable: true, get: () => true});
            document.dispatchEvent(new Event('visibilitychange'));
        }""")
        self.page.locator('#follow').click()
        self.page.wait_for_timeout(1200)
        self.assertEqual(counts['jobs'], paused_jobs)

        self.page.evaluate("""() => {
            Object.defineProperty(document, 'hidden', {configurable: true, get: () => false});
            document.dispatchEvent(new Event('visibilitychange'));
        }""")
        self.page.wait_for_timeout(250)
        self.assertEqual(counts['jobs'], paused_jobs + 1)
        self.page.locator('#follow').click()

    def test_project_switch_ignores_stale_fast_data_and_loads_progress_once(self):
        held = []
        beta_progress = []
        self.page.route('**/api/jobs?project=alpha', lambda route: held.append(route))
        self.page.route('**/api/activity?project=alpha', lambda route: held.append(route))
        self.page.route(
            '**/api/progress?project=beta',
            lambda route: (beta_progress.append(route.request.url), route.continue_()),
        )

        self.page.locator('#refresh').click()
        self.page.wait_for_timeout(100)
        self.assertEqual(len(held), 2)
        self.page.locator('#project').select_option('beta')
        expect(self.page.locator('#memory')).to_have_value('beta memory')
        for route in held:
            self._release(route)

        expect(self.page.locator('#wd-project')).to_have_text('beta')
        expect(self.page.locator('#work-map .wd-actor')).to_have_count(0)
        self.page.wait_for_timeout(250)
        self.assertEqual(len(beta_progress), 1)


if __name__ == '__main__':
    unittest.main()
