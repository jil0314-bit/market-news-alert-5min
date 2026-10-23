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


def big(n, extra=""):
    # 중요 기사(핵심 4사 출시 소식). 숫자가 달라 서로 다른 기사로 취급된다.
    return item(n, f"OpenAI releases GPT model update {n} {extra}".strip())


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
        with patch.object(ai,'send'),patch.object(ai,'PER_PRODUCT_LIMIT',99):
            result=self.run_fake([big(i) for i in range(1,11)])
        state=ai.load_state(self.path)
        self.assertEqual(result['sent'],ai.MAX_ITEMS)
        self.assertEqual(len(state['pending']),10-ai.MAX_ITEMS)
        self.assertEqual(len(state['sent']),2*ai.MAX_ITEMS)
        with patch.object(ai,'send'),patch.object(ai,'PER_PRODUCT_LIMIT',99):
            result=self.run_fake([big(i) for i in range(1,11)])
        self.assertEqual(result['sent'],10-ai.MAX_ITEMS)
        self.assertFalse(ai.load_state(self.path)['pending'])

    def test_minor_news_skipped_and_not_resent(self):
        minor=item(1,'국내 대학 인공지능 연구센터 개소식 열려')
        with patch.object(ai,'send') as send:
            result=self.run_fake([minor,big(2)])
        self.assertEqual(result['sent'],1)
        self.assertEqual(send.call_count,1)
        self.assertNotIn('연구센터',send.call_args.args[-1])
        state=ai.load_state(self.path)
        self.assertTrue(set(ai.keys_for(minor)) <= set(state['skipped']))
        with patch.object(ai,'send') as send:
            result=self.run_fake([minor])
        send.assert_not_called();self.assertEqual(result['sent'],0)

    def test_same_product_capped_and_numbered(self):
        items=[big(i) for i in range(1,6)]+[item(9,'Anthropic launches Claude coding update')]
        picked=ai.pick_items(items,ai.MAX_ITEMS,NOW)
        self.assertEqual(sum('OpenAI' in x['title'] for x in picked),ai.PER_PRODUCT_LIMIT)
        message=ai.format_pages(picked,NOW)[0][0]
        self.assertIn('1. <b>',message);self.assertIn('2. <b>',message);self.assertIn('3. <b>',message)
        self.assertIn('>원문</a>',message)

    def test_same_story_from_other_outlet_merged(self):
        c=item(3,'Anthropic launches Claude Duet personal agent for everyday work')
        d=item(4,'Anthropic unveils Claude Duet, an always-on personal assistant')
        e=item(5,'Anthropic rolls out Claude Duet to paid subscribers')
        f=item(6,'OpenAI releases GPT-6 on Google Cloud with new pricing')
        picked=ai.pick_items([c,d,e,f],ai.MAX_ITEMS,NOW)
        self.assertEqual(len(picked),2)
        duet=[x for x in picked if 'Duet' in x['title']][0]
        self.assertEqual(duet['coverage'],1)  # 같은 '예제 뉴스' 매체라 매체 수는 1
        self.assertEqual(len(ai.pick_items.dropped),2)
        # 12시간 안에 보낸 소식의 다른 매체 기사는 거른다 (한국어로 보냈어도)
        sent=[{'title':"앤트로픽, 개인 AI 에이전트 '클로드 듀엣' 공개",'title_ko':"앤트로픽, 개인 AI 에이전트 '클로드 듀엣' 공개",'sent_at':NOW.isoformat()}]
        g=item(7,"앤트로픽 '클로드 듀엣' 유료 구독자 대상 출시")
        self.assertFalse(ai.pick_items([g],ai.MAX_ITEMS,NOW,recent=sent))
        # 하루 지난 발송 기록은 막지 않는다
        sent[0]['sent_at']=(NOW-dt.timedelta(hours=20)).isoformat()
        self.assertEqual(len(ai.pick_items([g],ai.MAX_ITEMS,NOW,recent=sent)),1)

    def test_fluff_excluded(self):
        for title in ['Google Chrome cheat sheet: Gemini AI features','Who owns Claude AI? Anthropic investors','ChatGPT alternatives: 5 rivals','How to lock chats in ChatGPT']:
            self.assertFalse(ai.pick_items([item(1,title)],ai.MAX_ITEMS,NOW),title)
        junk=item(2,'OpenAI releases GPT model update 2');junk['source']='99Bitcoins'
        self.assertFalse(ai.pick_items([junk],ai.MAX_ITEMS,NOW))
        single=item(3,'Anthropic faces investor questions over compute spending')  # 출시 아님 + 검색 매체 1곳
        self.assertFalse(ai.pick_items([single],ai.MAX_ITEMS,NOW))

    def test_headline_cleanup_and_money_units(self):
        self.assertEqual(ai.clean_headline("앤트로픽 개인 AI 에이전트 경쟁 가세…'클로드 듀엣' 공개 By EBN"),"앤트로픽 개인 AI 에이전트 경쟁 가세…'클로드 듀엣' 공개")
        self.assertEqual(ai.MONEY.sub(ai.korean_money,'OpenAI new $30B raise at $1.25T valuation'),'OpenAI new 300억 달러 raise at 1.25조 달러 valuation')

    def test_similar_headlines_choose_official_and_keep_new_versions(self):
        a=item(1, 'Anthropic releases Claude 5 model for advanced coding tasks')
        b=item(2, 'Anthropic releases the Claude 5 model for advanced coding tasks')
        b['source_kind']='official'
        c=item(3, 'Anthropic releases Claude 6 model for advanced coding tasks')
        state=ai.load_state(self.path);ai.merge_pending(state,[a,b,c])
        self.assertEqual(len(state['pending']),2)
        self.assertIn(b['link'],[x['link'] for x in state['pending']])

    def test_sent_similar_story_suppressed_on_next_run(self):
        a=item(1, 'OpenAI launches new model for advanced coding workflows')
        b=item(2, 'OpenAI launches a new model for advanced coding workflows')
        with patch.object(ai,'send'):
            self.run_fake([a])
            result=self.run_fake([b])
        self.assertEqual(result['sent'],0)
        self.assertTrue(set(ai.keys_for(b)) <= set(ai.load_state(self.path)['sent']))

    def test_shared_company_does_not_hide_different_event(self):
        a=item(1, 'OpenAI launches new coding model for developers')
        b=item(2, 'OpenAI faces copyright lawsuit from book publishers')
        state=ai.load_state(self.path);ai.merge_pending(state,[a,b])
        self.assertEqual(len(state['pending']),2)

    def test_old_pending_expires_without_marking_sent(self):
        a=item();a['published_at']=(NOW-dt.timedelta(days=2)).isoformat()
        state=ai.load_state(self.path);state['pending']=[a];ai.save_state(self.path,state)
        with patch.object(ai,'send'):
            result=self.run_fake()
        self.assertEqual(result['sent'],0)
        self.assertFalse(ai.load_state(self.path)['pending'])
        self.assertFalse(ai.load_state(self.path)['sent'])

    def test_partial_delivery_tracks_only_confirmed_story_history(self):
        items=[big(i,f'filler{i}x '*30) for i in range(1,11)]
        for it in items:it['link']+='?long='+('x'*1000)
        with patch.object(ai,'PER_PRODUCT_LIMIT',99):
            first_group=ai.format_pages(ai.pick_items(items,ai.MAX_ITEMS,NOW),NOW)[0][1]
            with patch.object(ai,'send',side_effect=[None,RuntimeError('failed')]):
                with self.assertRaises(RuntimeError):self.run_fake(items)
        self.assertEqual(len(ai.load_state(self.path)['sent_stories']),len(first_group))

    def test_failure_does_not_mark_unsent(self):
        with patch.object(ai,'send',side_effect=RuntimeError('not accepted')):
            with self.assertRaises(RuntimeError):self.run_fake([big(1)])
        state=ai.load_state(self.path)
        self.assertFalse(state['sent']);self.assertEqual(len(state['pending']),1)
        self.assertIsNone(state['last_success_at'])

    def test_partial_page_success_checkpoint(self):
        items=[big(i,f'filler{i}x '*30) for i in range(1,11)]
        for it in items:it['link']+='?long='+('x'*1000)
        pages=ai.format_pages(items,NOW)
        self.assertGreater(len(pages),1)
        with patch.object(ai,'send',side_effect=[None,RuntimeError('second failed')]),patch.object(ai,'PER_PRODUCT_LIMIT',99):
            with self.assertRaises(RuntimeError):self.run_fake(items)
        state=ai.load_state(self.path)
        self.assertGreater(len(state['sent']),0)
        self.assertGreater(len(state['pending']),0)
        self.assertEqual(len(state['sent'])//2+len(state['pending']),len(items))

    def test_dry_run_no_send_no_state(self):
        with patch.object(ai,'send') as send:
            result=self.run_fake([big(1)],dry_run=True)
        send.assert_not_called();self.assertFalse(self.path.exists());self.assertEqual(len(result['items']),1)

    def test_no_news_sends_nothing(self):
        with patch.object(ai,'send') as send:self.run_fake()
        send.assert_not_called()
        state=ai.load_state(self.path);self.assertEqual(state['last_success_at'],NOW.isoformat())
        with patch.object(ai,'send') as send:self.run_fake(failures=['AI'])
        send.assert_not_called()

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
        self.assertEqual(self.session.post.call_args.kwargs['json']['parse_mode'],'HTML')
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
        self.assertEqual(len(items),len(ai.intel.sources(ai.TOPICS)));self.assertFalse(failures)
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
