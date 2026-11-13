"""Synthetic fixed-location weather contracts; no live weather/model/QQ traffic."""
import asyncio
from datetime import datetime, timedelta
import importlib
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from maibot_sdk.components import collect_components

from test_support import TestPlugin, plugin

weather = importlib.import_module(plugin.__package__ + '.weather')
NOW = datetime(2026, 10, 25, 12, tzinfo=weather.TZ)


def fixture(now=NOW):
    return {'latitude':10.0, 'longitude':20.0, 'timezone':'Asia/Shanghai', 'utc_offset_seconds':28800,
        'current_units':{k:weather.UNITS[k] for k in weather.CURRENT},
        'daily_units':{k:weather.UNITS[k] for k in weather.DAILY},
        'current':{'time':now.strftime('%Y-%m-%dT%H:%M'), 'temperature_2m':0.0,
                   'apparent_temperature':-2, 'relative_humidity_2m':50, 'weather_code':0, 'wind_speed_10m':3},
        'daily':{'time':[(now.date()+timedelta(days=i)).isoformat() for i in range(3)],
                 'temperature_2m_max':[10,11,12], 'temperature_2m_min':[-2,-1,0],
                 'precipitation_probability_max':[0,20,40], 'precipitation_sum':[0,1,2],
                 'weather_code':[0,3,61]}}


def settings():
    return SimpleNamespace(enabled=True, location_name='合成地点', latitude=10.0, longitude=20.0)


class WeatherServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = [100]
        self.wall = [NOW]
        self.fetch = AsyncMock(return_value=fixture())
        self.service = weather.WeatherService(fetch=self.fetch, clock=lambda:self.clock[0], now=lambda:self.wall[0])
        self.config = settings()
        self.options = {'trust_env':False}

    async def test_today_current_and_forecast_are_distinct_with_source_time_and_no_false_geolocation(self):
        result = await self.service.query(self.config,0,self.options)
        self.assertTrue(result['success'])
        self.assertIn('当前模型天气：晴；气温0℃',result['content'])
        self.assertIn('日预报',result['content'])
        self.assertIn('地点：合成地点（管理员配置',result['content'])
        self.assertIn('当前数据有效时间',result['content'])
        self.assertIn('CC BY 4.0',result['content'])
        self.assertIn('天气模型数据，非气象站实测',result['content'])
        self.assertIn('查询时间',result['content'])
        self.assertNotIn('缓存',result['content'])
        self.assertFalse(result['cache_hit'])
        self.fetch.assert_awaited_once_with(10.0,20.0,self.options)

    async def test_three_day_queries_share_success_cache_but_do_not_repeat_current_as_tomorrow(self):
        today = await self.service.query(self.config,0,self.options)
        tomorrow = await self.service.query(self.config,1,self.options)
        self.assertTrue(tomorrow['cache_hit'])
        self.assertIn('缓存命中',tomorrow['content'])
        self.assertEqual(tomorrow['forecast_date'],'2026-10-26')
        self.assertNotIn('当前模型天气：',tomorrow['content'])
        self.assertIn('最高11℃',tomorrow['content'])
        self.assertEqual(today['fetched_at'],tomorrow['fetched_at'])
        tomorrow['content']='caller changed result'
        again=await self.service.query(self.config,2,self.options)
        self.assertIn('最高12℃',again['content'])
        self.fetch.assert_awaited_once()

    async def test_cache_expires_in_five_minutes_and_never_refreshes_without_a_call(self):
        await self.service.query(self.config,0,self.options)
        self.clock[0]+=301
        self.fetch.assert_awaited_once()
        result=await self.service.query(self.config,0,self.options)
        self.assertFalse(result['cache_hit'])
        self.assertEqual(self.fetch.await_count,2)

    async def test_cache_key_includes_coordinates_label_proxy_and_local_day(self):
        await self.service.query(self.config,0,self.options)
        self.config.location_name='另一个合成名称'
        await self.service.query(self.config,0,self.options)
        await self.service.query(self.config,0,{'trust_env':False,'proxy':'http://127.0.0.1:7890'})
        self.wall[0]+=timedelta(days=1)
        self.fetch.return_value=fixture(self.wall[0])
        result=await self.service.query(self.config,0,self.options)
        self.assertEqual(result['forecast_date'],'2026-10-26')
        self.assertEqual(self.fetch.await_count,4)

    async def test_invalid_day_or_location_never_requests(self):
        for day in [-1,3,True,'1',None]:
            with self.subTest(day=day),self.assertRaises(weather.WeatherError):
                await self.service.query(self.config,day,self.options)
        for lat,lon in [(float('nan'),20),(10,float('inf')),(91,20),(10,181),(True,20)]:
            self.config.latitude,self.config.longitude=lat,lon
            with self.assertRaises(weather.WeatherError):
                await self.service.query(self.config,0,self.options)
        self.config=settings();self.config.location_name=''
        with self.assertRaises(weather.WeatherError):
            await self.service.query(self.config,0,self.options)
        self.fetch.assert_not_awaited()

    async def test_temporary_failure_is_not_fake_success_or_automatic_retry(self):
        self.fetch.side_effect=weather.WeatherError('timeout','safe')
        with self.assertRaises(weather.WeatherError):
            await self.service.query(self.config,0,self.options)
        with self.assertRaises(weather.WeatherError) as raised:
            await self.service.query(self.config,0,self.options)
        self.assertEqual(raised.exception.code,'backoff')
        self.fetch.assert_awaited_once()
        self.clock[0]+=61
        self.fetch.side_effect=None
        self.assertTrue((await self.service.query(self.config,0,self.options))['success'])

    async def test_rate_limit_backoff_is_respected(self):
        self.fetch.side_effect=weather.WeatherError('rate_limited','safe',cooldown=300)
        with self.assertRaises(weather.WeatherError):
            await self.service.query(self.config,0,self.options)
        self.clock[0]+=120
        with self.assertRaises(weather.WeatherError) as raised:
            await self.service.query(self.config,0,self.options)
        self.assertEqual(raised.exception.code,'backoff')
        self.fetch.assert_awaited_once()

    async def test_concurrent_query_is_bounded_no_second_request_or_cancelled_cache(self):
        started=asyncio.Event()
        async def wait(*args):
            started.set();await asyncio.Event().wait()
        self.fetch.side_effect=wait
        job=asyncio.create_task(self.service.query(self.config,0,self.options))
        await started.wait()
        with self.assertRaises(weather.WeatherError) as raised:
            await self.service.query(self.config,0,self.options)
        self.assertEqual(raised.exception.code,'busy')
        job.cancel()
        with self.assertRaises(asyncio.CancelledError):await job
        self.assertIsNone(self.service.cache)
        self.assertFalse(self.service.lock.locked())

    async def test_stale_cached_current_is_never_passed_as_fresh(self):
        await self.service.query(self.config,0,self.options)
        self.wall[0]+=timedelta(hours=3)
        with self.assertRaises(weather.WeatherError) as raised:
            await self.service.query(self.config,0,self.options)
        self.assertEqual(raised.exception.code,'stale')


class WeatherParsingTests(unittest.TestCase):
    def test_bad_units_coordinates_timezone_and_stale_time_fail_closed(self):
        for mutate in [lambda p:p.update(timezone='UTC'),lambda p:p.update(latitude=30),
                       lambda p:p['current_units'].update(temperature_2m='°F'),
                       lambda p:p['current'].update(time='2026-10-24T12:00'),
                       lambda p:p['daily'].update(time=['2026-10-24']*3),
                       lambda p:p['daily'].update(temperature_2m_max=[1]),
                       lambda p:p['daily'].update(precipitation_probability_max=[101,0,0]),
                       lambda p:p['current'].update(temperature_2m=float('nan')),
                       lambda p:p['current'].update(weather_code=999),
                       lambda p:p['daily'].update(temperature_2m_min=[40,40,40])]:
            payload=fixture();mutate(payload)
            with self.subTest(payload=payload),self.assertRaises(weather.WeatherError):
                weather.parse_weather(payload,10,20,NOW)

    def test_missing_optional_values_stay_unknown_not_zero(self):
        payload=fixture();payload['current']['apparent_temperature']=None
        payload['daily']['precipitation_probability_max']=[None,None,None]
        parsed=weather.parse_weather(payload,10,20,NOW)
        rendered=weather.render_weather(parsed,'合成地点',0,NOW.isoformat(),False)
        self.assertIn('体感未知',rendered)
        self.assertIn('降水概率最大值未知',rendered)
        self.assertIn('气温0℃',rendered)


class WeatherHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def run_fetch(self,handler):
        calls=[]
        original=httpx.AsyncClient
        def factory(**kwargs):
            calls.append(kwargs)
            return original(transport=httpx.MockTransport(handler),**kwargs)
        with patch.object(weather.httpx,'AsyncClient',side_effect=factory):
            result=await weather.fetch_weather(10,20,{'trust_env':False})
        return result,calls

    async def test_fixed_endpoint_units_no_cookies_auth_prompt_or_geocoding(self):
        requests=[]
        def handler(request):
            requests.append(request)
            return httpx.Response(200,json=fixture())
        result,calls=await self.run_fetch(handler)
        self.assertEqual(result,fixture())
        request=requests[0]
        self.assertEqual(len(requests),1)
        self.assertEqual(request.url.host,'api.open-meteo.com')
        self.assertEqual(request.url.path,'/v1/forecast')
        self.assertFalse(calls[0]['follow_redirects'])
        self.assertFalse(calls[0]['trust_env'])
        self.assertNotIn('cookie',request.headers)
        self.assertNotIn('authorization',request.headers)
        self.assertEqual(request.content,b'')
        self.assertEqual(request.url.params['forecast_days'],'3')
        self.assertEqual(request.url.params['timezone'],'Asia/Shanghai')
        self.assertEqual(request.url.params['wind_speed_unit'],'kmh')
        self.assertNotIn('apikey',request.url.params)

    async def test_http_error_redirect_invalid_json_and_oversize_are_not_retried(self):
        for response in [httpx.Response(302,headers={'location':'https://evil.invalid'}),
                         httpx.Response(500,text='PRIVATE'),httpx.Response(200,text='PRIVATE'),
                         httpx.Response(200,content=b'x'*(weather.MAX_BYTES+1))]:
            seen=[]
            with self.subTest(status=response.status_code),self.assertRaises(weather.WeatherError) as raised:
                await self.run_fetch(lambda req:seen.append(req) or response)
            self.assertEqual(len(seen),1)
            self.assertNotIn('PRIVATE',str(raised.exception))

    async def test_http_429_keeps_retry_after_without_disclosing_provider_body(self):
        with self.assertRaises(weather.WeatherError) as raised:
            await self.run_fetch(lambda _:httpx.Response(429,headers={'retry-after':'300'},text='PRIVATE'))
        self.assertEqual(raised.exception.cooldown,300)
        self.assertEqual(raised.exception.code,'rate_limited')
        self.assertNotIn('PRIVATE',str(raised.exception))


class WeatherAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.p=TestPlugin();await self.p.on_load()
        self.addAsyncCleanup(self.p.on_unload)
        self.p.config.weather.enabled=True
        self.p.config.weather.location_name='合成地点'
        self.p.config.weather.latitude=10
        self.p.config.weather.longitude=20
        self.p._weather=weather.WeatherService(fetch=AsyncMock(return_value=fixture()),now=lambda:NOW)

    async def test_real_sdk_has_short_tool_and_no_model_location_override_or_auto_work(self):
        tools=[x for x in collect_components(self.p) if x['type']=='TOOL']
        entry=next(x for x in tools if x['name']=='neko_web_weather')['metadata']
        self.assertLessEqual(len(entry['description']),45)
        self.assertIn('不定位',entry['description'])
        self.assertEqual([p['name'] for p in entry['parameters']],['day'])
        self.p._weather.fetch.assert_not_awaited()
        result=await self.p.handle_weather(day=0,stream_id='synthetic-scope')
        self.assertTrue(result['success'])
        self.p.ctx.send.image.assert_not_awaited()
        self.assertFalse(hasattr(self.p.ctx,'llm'))

    async def test_default_and_config_scope_are_disabled_without_new_account_or_provider(self):
        self.assertFalse(plugin.NekoWebPluginConfig().weather.enabled)
        self.p.config.weather.enabled=False
        self.assertEqual((await self.p.handle_weather())['status'],'disabled')
        self.p._weather.fetch.assert_not_awaited()
        self.p.config.weather.enabled=True
        self.p.config.plugin.enabled=False
        self.assertEqual((await self.p.handle_weather())['status'],'disabled')
        self.p._weather.fetch.assert_not_awaited()

    async def test_model_cannot_silently_change_city_or_coordinates(self):
        for kwargs in [{'city':'elsewhere'},{'latitude':20},{'longitude':20},{'location':'elsewhere'}]:
            self.assertEqual((await self.p.handle_weather(**kwargs))['status'],'invalid_request')
        self.p._weather.fetch.assert_not_awaited()

    async def test_config_revoke_during_request_discards_old_result(self):
        async def change(*args):
            self.p.config.weather.enabled=False
            return fixture()
        self.p._weather.fetch.side_effect=change
        self.assertEqual((await self.p.handle_weather())['status'],'config_changed')

    async def test_unload_cancels_inflight_fetch_and_never_adds_background_refresh(self):
        started=asyncio.Event()
        async def wait(*args):started.set();await asyncio.Event().wait()
        self.p._weather.fetch.side_effect=wait
        job=asyncio.create_task(self.p.handle_weather())
        await started.wait()
        await self.p.on_unload()
        with self.assertRaises(asyncio.CancelledError):await job
        self.assertFalse(self.p._work.tasks)
        self.assertIsNone(self.p._weather.cache)

    async def test_weather_error_does_not_trigger_search_provider_fallback(self):
        self.p._provider_search=AsyncMock()
        self.p._weather.fetch.side_effect=weather.WeatherError('unavailable','safe')
        result=await self.p.handle_weather()
        self.assertFalse(result['success'])
        self.p._provider_search.assert_not_awaited()
