import datetime as dt
import unittest
from unittest.mock import Mock
import ai_news_intelligence as intel
import ai_news_translation as tr
import ai_news_digest as ai

NOW=dt.datetime(2026,10,18,3,tzinfo=dt.timezone.utc)
def item(title,**kw):
    return dict(title=title,source='예제',link='https://example.com/a',published_at=NOW.isoformat(),**kw)


class IntelligenceTests(unittest.TestCase):
    def test_priority_target_model_launch_beats_market_noise(self):
        main=item('Anthropic releases Claude Sonnet 5.5 coding model')
        noise=item('AI stock price falls after earnings')
        self.assertLess(intel.order_key(main),intel.order_key(noise))
        self.assertEqual(intel.profile(main)['status'],'출시·업데이트 보도')
        self.assertIn('코딩·개발',intel.profile(main)['features'])

    def test_upcoming_rumor_beta_never_presented_as_released(self):
        for title,status in [('Grok 9 coming soon','출시 예정·예고'),('Rumor: OpenAI releases GPT-99','미확인 보도·전망'),('Gemini preview now available','미리보기·베타'),('Claude is not yet available','출시 예정·예고')]:
            self.assertEqual(intel.profile(item(title))['status'],status)

    def test_cursor_suno_chinese_tools_no_ai_word_needed(self):
        for title in ['Cursor adds new agent workflow','Suno launches Studio music editor','通义千问发布新模型，支持视频与代码','Kimi K2 release','MiniMax new speech model','可灵推出视频模型']:
            self.assertTrue(intel.RELEVANT.search(title),title)
        self.assertTrue(intel.profile(item('通义千问发布新模型'))['china'])
        self.assertIn('음악·작곡',intel.profile(item('Suno launches music model'))['features'])

    def test_relevant_title_requires_no_generic_ai_matching(self):
        self.assertFalse(intel.RELEVANT.search('daily rainfall'))

    def test_direct_and_indexed_sources_differ(self):
        sources=intel.sources(ai.TOPICS)
        self.assertTrue(any(s['name']=='OpenAI 공식 RSS' and s['kind']=='official' for s in sources))
        self.assertTrue(any(s['region']=='CN' for s in sources))
        self.assertTrue(any('anthropic.com' in s['url'] and s['kind']=='official_search' for s in sources))
        direct='\n'.join(s['url'] for s in sources if s['kind']=='specialist')
        self.assertIn('aitimes.com',direct);self.assertIn('techcrunch.com',direct)
        self.assertNotIn('qbitai.com/feed',direct)

    def test_china_and_tool_slots_survive_busy_feed(self):
        news=[item(f'OpenAI releases GPT-{n}') for n in range(1,45)]
        china=[item(f'Qwen {n} 发布',region='CN') for n in range(1,7)]
        tools=[item(f'Suno {n} launches music editor') for n in range(1,6)]
        picked=intel.select_items(news+china+tools,30,NOW)
        self.assertEqual(len(picked),30)
        self.assertGreaterEqual(sum(intel.profile(x)['china'] for x in picked),5)
        self.assertGreaterEqual(sum(intel.profile(x)['tool'] for x in picked),4)

    def test_integration_needs_multiple_products_or_explicit_connector(self):
        self.assertTrue(intel.profile(item('Cursor integrates Claude through MCP'))['integration'])
        self.assertFalse(intel.profile(item('Claude adds support for math'))['integration'])
        self.assertTrue(intel.profile(item('Suno adds an API integration'))['integration'])

    def test_unknown_capabilities_and_inference_label(self):
        lines='\n'.join(intel.briefing_lines(item('OpenAI announces new model')))
        self.assertIn('구체 기능 미확인',lines)
        self.assertIn('활용 검토(추정)',lines)
        self.assertNotIn('코딩에 가장',lines)

    def test_duplicate_prefers_official_source_evidence(self):
        a=item('OpenAI releases model',source_kind='search')
        b=item('OpenAI releases model',source_kind='official',excerpt='New reasoning model')
        state={'sent':{},'pending':[]}
        ai.merge_pending(state,[a,b])
        self.assertEqual(len(state['pending']),1)
        self.assertEqual(state['pending'][0]['source_kind'],'official')


class TranslationTests(unittest.TestCase):
    def test_context_translation_keeps_model_version_numbers(self):
        it=item('Claude Sonnet 5.5 launches today',excerpt='The coding agent is 30% faster.')
        def engine(text):
            return text.replace('launches today','오늘 출시됩니다').replace('The ','').replace(' is ','는 ').replace(' faster.',' 더 빠릅니다.')
        result=tr.translated_record(it,engine)
        self.assertEqual(result['quality'],'자동번역·보존검사 통과')
        self.assertIn('Claude Sonnet 5.5',result['title'])
        self.assertIn('30%',result['excerpt']);self.assertIn('코딩 에이전트',result['excerpt'])

    def test_changed_token_missing_body_and_added_number_rejected(self):
        it=item('Claude 5 launches soon',excerpt='It supports coding.')
        for engine in [lambda text:'클로드 모델이 출시됩니다',lambda text:text.replace('§§§',''),lambda text:text+' 100배 빨라집니다']:
            result=tr.translated_record(it,engine)
            self.assertEqual(result['quality'],'번역 미검수·원문 표시')
            self.assertEqual(result['title'],it['title'])

    def test_chinese_translated_without_leftover_clause(self):
        it=item('Qwen 3 发布新模型',excerpt='支持代码分析')
        result=tr.translated_record(it,lambda text:text.replace('发布新模型','새 모델을 공개했습니다').replace('支持代码分析','코드 분석을 지원합니다'))
        self.assertEqual(result['quality'],'자동번역·보존검사 통과')
        self.assertNotIn('发布',result['title']);self.assertIn('Qwen 3',result['title'])

    def test_negation_loss_fails_check(self):
        it=item('Claude does not support video')
        bad=lambda text:text.replace('does not support video','동영상을 지원합니다')
        self.assertEqual(tr.translated_record(it,bad)['quality'],'번역 미검수·원문 표시')
        good=lambda text:text.replace('does not support video','동영상을 지원하지 않습니다')
        self.assertEqual(tr.translated_record(it,good)['quality'],'자동번역·보존검사 통과')

    def test_korean_not_retranslated(self):
        engine=Mock()
        result=tr.translated_record(item('클로드의 새로운 기능이 공개됐습니다'),engine)
        engine.assert_not_called();self.assertEqual(result['quality'],'원문 한국어')

    def test_garbled_or_partial_english_stays_unverified(self):
        it=item('OpenAI releases a new model for research tasks')
        self.assertEqual(tr.translated_record(it,lambda text:text)['quality'],'번역 미검수·원문 표시')


if __name__=='__main__':unittest.main()
