import copy
import datetime as dt
import json
from pathlib import Path
import tempfile
import time
import types
import unittest
from unittest.mock import Mock, patch

import requests
import yaml
import ai_news_digest as ai

NOW = dt.datetime(2026,10,18,2,0,tzinfo=ai.UTC)


def item(n=1, title=None):
    return {"title":title or f"AI 모델 {n} 출시", "source":"예제 뉴스", "link":f"https://example.com/news/{n}",
            "published_at":(NOW-dt.timedelta(minutes=n)).isoformat(), "topic":"AI 전반"}


class DigestTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'ai_news_state.json'
        self.session=Mock();self.session.headers={}

    def run_fake(self, items=None, failures=(), **kwargs):
        with patch.object(ai,'collect',return_value=(copy.deepcopy(items or []),list(failures))),patch.dict(ai.os.environ,{'TELEGRAM_BOT_TOKEN':'test-token','TELEGRAM_CHAT_ID':'test-chat'}),patch.object(ai.time,'sleep'):
            return ai.run(self.path,session=self.session,now=NOW,translate=lambda t:t,**kwargs)

    def test_empty_state_and_atomic_save(self):
        state=ai.load_state(self.path);ai.save_state(self.path,state)
        self.assertEqual(ai.load_state(self.path),state)
        self.assertFalse(list(self.path.parent.glob('*.tmp')))

    def test_corrupt_history_refuses_to_reset(self):
        self.path.write_text('{broken',encoding='utf-8')
        with self.assertRaises(ValueError):ai.load_state(self.path)
        self.path.write_text('{}',encoding='utf-8')
        with self.assertRaises(ValueError):ai.load_state(self.path)

    def test_dedupe_title_url_and_tracking(self):
        a=item();b=item(2,title=a['title']);c=item(3);c['link']=a['link']+'?utm_source=x'
        state=ai.load_state(self.path);ai.merge_pending(state,[a,b,c])
        self.assertEqual(len(state['pending']),1)
        keys=ai.keys_for(a)
        state['sent']={keys[0]:NOW.isoformat()};state['pending']=[]
        ai.merge_pending(state,[a]);self.assertFalse(state['pending'])

    def test_backlog_retained_after_success_and_deduped_next_run(self):
        with patch.object(ai,'send') as send:
            result=self.run_fake([item(i) for i in range(1,36)])
        state=ai.load_state(self.path)
        self.assertEqual(result['sent'],30)
        self.assertEqual(len(state['pending']),5)
        self.assertEqual(len(state['sent']),60)
        with patch.object(ai,'send'):
            result=self.run_fake([item(i) for i in range(1,36)])
        self.assertEqual(result['sent'],5)
        self.assertFalse(ai.load_state(self.path)['pending'])

    def test_failure_does_not_mark_unsent(self):
        with patch.object(ai,'send',side_effect=RuntimeError('not accepted')):
            with self.assertRaises(RuntimeError):self.run_fake([item()])
        state=ai.load_state(self.path)
        self.assertFalse(state['sent']);self.assertEqual(len(state['pending']),1)
        self.assertIsNone(state['last_success_at'])

    def test_partial_page_success_checkpoint(self):
        items=[item(i,title='AI '+('테스트 '*50)+str(i)) for i in range(1,11)]
        for it in items:it['link']+='?long='+('x'*1000)
        pages=ai.format_pages(items,NOW)
        self.assertGreater(len(pages),1)
        with patch.object(ai,'send',side_effect=[None,RuntimeError('second failed')]):
            with self.assertRaises(RuntimeError):self.run_fake(items)
        state=ai.load_state(self.path)
        self.assertGreater(len(state['sent']),0)
        self.assertGreater(len(state['pending']),0)
        self.assertEqual(len(state['sent'])//2+len(state['pending']),len(items))

    def test_dry_run_no_send_no_state(self):
        with patch.object(ai,'send') as send:
            result=self.run_fake([item()],dry_run=True)
        send.assert_not_called();self.assertFalse(self.path.exists());self.assertEqual(len(result['items']),1)

    def test_no_news_notification_and_failed_feed_qualifier(self):
        with patch.object(ai,'send') as send:self.run_fake()
        self.assertIn('새 AI 뉴스가 없습니다',send.call_args.args[-1])
        state=ai.load_state(self.path);self.assertEqual(state['last_success_at'],NOW.isoformat())
        with patch.object(ai,'send') as send:self.run_fake(failures=['AI'])
        self.assertIn('확인하지 못했습니다',send.call_args.args[-1])

    def test_empty_first_run_lookback_and_partial_failure_watermark(self):
        with patch.object(ai,'collect',return_value=([],['broken'])) as collect,patch.object(ai,'send'),patch.dict(ai.os.environ,{'TELEGRAM_BOT_TOKEN':'t','TELEGRAM_CHAT_ID':'c'}):
            ai.run(self.path,session=self.session,now=NOW,translate=lambda t:t)
        self.assertEqual(collect.call_args.args[-1],NOW-dt.timedelta(hours=2,minutes=30))
        self.assertIsNone(ai.load_state(self.path)['last_success_at'])

    def test_message_utf16_limit_emoji_and_long_link(self):
        items=[item(i,title='AI '+('🤖'*200)) for i in range(1,20)]
        for it in items:it['link']+='?q='+('x'*3000)
        pages=ai.format_pages(items,NOW,backlog=5,failures=['one'])
        self.assertTrue(all(ai.utf16_length(message)<=3800 for message,_ in pages))
        self.assertEqual(sum(len(group) for _,group in pages),len(items))

    def test_telegram_acceptance_and_secret_safe_failure(self):
        response=Mock(ok=True);response.json.return_value={'ok':True,'result':{'message_id':1}}
        self.session.post.return_value=response
        ai.send(self.session,'private-token','private-chat','news')
        self.assertNotIn('parse_mode',self.session.post.call_args.kwargs['json'])
        self.session.post.side_effect=requests.Timeout('https://api.telegram.org/botprivate-token')
        with self.assertRaises(RuntimeError) as ctx:ai.send(self.session,'private-token','private-chat','news')
        self.assertNotIn('private-token',str(ctx.exception))
        self.session.post.side_effect=None;response.json.return_value={'ok':False}
        with self.assertRaises(RuntimeError):ai.send(self.session,'t','c','news')

    def test_links_and_relevance_boundary(self):
        for url in ['javascript:alert(1)','file:///x','https://user:pass@example.com']:
            self.assertEqual(ai.safe_link(url),'')
        for title in ['OpenAI model release','국내 인공지능 연구','AI safety update','엔비디아 AI 칩']:
            self.assertTrue(ai.AI_PATTERN.search(title))
        for title in ['daily rainfall','retail earnings','airport arrivals']:
            self.assertFalse(ai.AI_PATTERN.search(title))

    def test_feed_dates_filter_missing_future_old_non_ai(self):
        entries=[]
        for title,when in [('AI launch',NOW),('AI future',NOW+dt.timedelta(days=1)),('AI old',NOW-dt.timedelta(days=2)),('daily rain',NOW)]:
            entries.append({'title':title,'link':'https://example.com/'+title,'published_parsed':when.utctimetuple()})
        entries.append({'title':'AI missing date','link':'https://example.com/nodate'})
        response=Mock();response.content=b'<rss/>';self.session.get.return_value=response
        parsed={'version':'rss20','entries':entries}
        class Feed(dict):
            @property
            def entries(self):return self['entries']
        with patch.object(ai.feedparser,'parse',return_value=Feed(parsed)):
            items,failures=ai.collect(self.session,NOW,NOW-dt.timedelta(hours=3))
        self.assertEqual(len(items),len(ai.TOPICS));self.assertFalse(failures)
        self.assertTrue(all(it['title']=='AI launch' for it in items))

    def test_all_feed_failure_is_error(self):
        self.session.get.side_effect=requests.Timeout
        with self.assertRaises(RuntimeError):ai.collect(self.session,NOW,NOW-dt.timedelta(hours=3))

    def test_translation_failure_keeps_original(self):
        fake=types.ModuleType('deep_translator');fake.GoogleTranslator=Mock(side_effect=RuntimeError)
        with patch.dict('sys.modules',{'deep_translator':fake}):
            self.assertEqual(ai.translate_title('AI research release'),'AI research release')

    def test_workflow_only_new_job_and_two_hour_schedule(self):
        flow=yaml.load(Path('.github/workflows/ai-news-digest.yml').read_text(encoding='utf-8'),Loader=yaml.BaseLoader)
        self.assertEqual(flow['on']['schedule'][0]['cron'],'37 */2 * * *')
        self.assertEqual(flow['concurrency']['cancel-in-progress'],'false')
        text=Path('.github/workflows/ai-news-digest.yml').read_text(encoding='utf-8')
        self.assertNotIn('morning_us_market_report.py',text)
        self.assertNotIn('seen_news.sqlite3',text)
        self.assertIn('if: always()',text)


if __name__=='__main__':unittest.main()
