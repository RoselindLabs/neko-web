"""On-demand fixed-location weather. No model, geolocation, timer or credentials."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import math
import time

import httpx

ENDPOINT = 'https://api.open-meteo.com/v1/forecast'
SOURCE = 'https://open-meteo.com/'
# Only current/next-two-day queries in Beijing time; no historical DST/tzdata dependency.
TZ = timezone(timedelta(hours=8), 'Asia/Shanghai')
MAX_BYTES = 256 * 1024
CACHE_SECONDS = 300
CURRENT = ('temperature_2m', 'apparent_temperature', 'relative_humidity_2m',
           'weather_code', 'wind_speed_10m')
DAILY = ('temperature_2m_max', 'temperature_2m_min', 'precipitation_probability_max',
         'precipitation_sum', 'weather_code')
UNITS = {'temperature_2m': '°C', 'apparent_temperature': '°C',
         'relative_humidity_2m': '%', 'wind_speed_10m': 'km/h', 'weather_code': 'wmo code',
         'temperature_2m_max': '°C', 'temperature_2m_min': '°C',
         'precipitation_probability_max': '%', 'precipitation_sum': 'mm'}
CODES = {0:'晴', 1:'大部晴朗', 2:'局部多云', 3:'阴', 45:'雾', 48:'雾凇',
         51:'小毛毛雨', 53:'中等毛毛雨', 55:'强毛毛雨', 56:'轻度冻毛毛雨', 57:'强冻毛毛雨',
         61:'小雨', 63:'中雨', 65:'大雨', 66:'轻度冻雨', 67:'强冻雨',
         71:'小雪', 73:'中雪', 75:'大雪', 77:'雪粒',
         80:'小阵雨', 81:'中等阵雨', 82:'强阵雨', 85:'小阵雪', 86:'强阵雪',
         95:'雷暴', 96:'雷暴伴小冰雹', 99:'雷暴伴强冰雹'}


class WeatherError(ValueError):
    def __init__(self, code, message, cooldown=60):
        super().__init__(message)
        self.code, self.cooldown = code, cooldown


def location(config):
    name = config.location_name.strip()
    if not name or len(name) > 80 or any(ord(c) < 32 for c in name):
        raise WeatherError('config_error', '请管理员设置天气地点名称和WGS84经纬度。')
    lat, lon = config.latitude, config.longitude
    if (isinstance(lat, bool) or isinstance(lon, bool)
            or not isinstance(lat, (int, float)) or not isinstance(lon, (int, float))
            or not math.isfinite(lat) or not math.isfinite(lon)
            or not -90 <= lat <= 90 or not -180 <= lon <= 180):
        raise WeatherError('config_error', '天气经纬度无效，请管理员检查。')
    return name, float(lat), float(lon)


def local_time(value):
    if not isinstance(value, str):
        raise WeatherError('invalid_response', '天气数据时间无效。')
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            result = result.replace(tzinfo=TZ)
        return result.astimezone(TZ)
    except ValueError:
        raise WeatherError('invalid_response', '天气数据时间无效。') from None


def number(value, field):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise WeatherError('invalid_response', '天气数据包含无效数值。')
    low, high = (-100, 70) if 'temperature' in field else (0, 100000)
    if field in {'relative_humidity_2m', 'precipitation_probability_max'}:
        low, high = 0, 100
    if field == 'weather_code' and (type(value) is not int or value not in CODES):
        raise WeatherError('invalid_response', '天气现象代码无法识别。')
    if not low <= value <= high:
        raise WeatherError('invalid_response', '天气数值超出合理范围。')
    return value


def parse_weather(payload, lat, lon, now):
    if not isinstance(payload, dict) or payload.get('error'):
        raise WeatherError('invalid_response', '天气源未返回有效数据。')
    if payload.get('timezone') != 'Asia/Shanghai' or payload.get('utc_offset_seconds') != 28800:
        raise WeatherError('invalid_response', '天气响应时区不匹配，未套用北京时间标签。')
    for key, expected in [('latitude', lat), ('longitude', lon)]:
        got = payload.get(key)
        if (isinstance(got, bool) or not isinstance(got, (int, float))
                or not math.isfinite(got) or abs(got - expected) > 1):
            raise WeatherError('invalid_response', '天气响应地点与配置坐标不匹配。')
    current, daily = payload.get('current'), payload.get('daily')
    if not isinstance(current, dict) or not isinstance(daily, dict):
        raise WeatherError('invalid_response', '天气响应缺少当前数据或日预报。')
    for group, fields in [('current', CURRENT), ('daily', DAILY)]:
        units = payload.get(group + '_units')
        if not isinstance(units, dict) or any(units.get(key) != UNITS[key] for key in fields):
            raise WeatherError('invalid_response', '天气响应单位不匹配。')
    valid_at = local_time(current.get('time'))
    if not -600 <= (now - valid_at).total_seconds() <= 7200:
        raise WeatherError('stale', '天气源当前数据过旧或时间异常，未当作实时天气。')
    dates = daily.get('time')
    expected = [(now.date() + timedelta(days=i)).isoformat() for i in range(3)]
    if dates != expected:
        raise WeatherError('invalid_response', '天气预报日期不匹配，未把旧预报当作今天。')
    if any(not isinstance(daily.get(key), list) or len(daily[key]) != 3 for key in DAILY):
        raise WeatherError('invalid_response', '天气预报字段缺失或长度不匹配。')
    normalized = {'current_time': valid_at.isoformat(),
                  'current': {key:number(current.get(key), key) for key in CURRENT}, 'daily': []}
    for i, date in enumerate(dates):
        row = {key:number(daily[key][i], key) for key in DAILY}
        lo, hi = row['temperature_2m_min'], row['temperature_2m_max']
        if lo is not None and hi is not None and lo > hi:
            raise WeatherError('invalid_response', '天气预报最高最低温度矛盾。')
        normalized['daily'].append({'date':date, **row})
    if normalized['current']['temperature_2m'] is None:
        raise WeatherError('unavailable', '天气源缺少当前气温，未生成确定的天气结论。')
    return normalized


async def fetch_weather(lat, lon, client_options):
    params = {'latitude':lat, 'longitude':lon, 'current':','.join(CURRENT),
              'daily':','.join(DAILY), 'forecast_days':3, 'timezone':'Asia/Shanghai',
              'temperature_unit':'celsius', 'wind_speed_unit':'kmh', 'precipitation_unit':'mm'}
    try:
        # Only a fixed provider URL, never a model-provided URL, headers, key or cookie.
        async with httpx.AsyncClient(timeout=8, follow_redirects=False, **client_options) as client:
            async with client.stream('GET', ENDPOINT, params=params,
                                     headers={'User-Agent':'neko-web-weather/1.10.0'}) as response:
                if response.status_code == 429:
                    retry = response.headers.get('retry-after', '')
                    cooldown = max(60, min(int(retry), 86400)) if retry.isdecimal() and len(retry) <= 6 else 120
                    raise WeatherError('rate_limited', '天气源限流。', cooldown)
                if response.status_code != 200:
                    raise WeatherError('unavailable', '天气源请求未成功。')
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(raw) + len(chunk) > MAX_BYTES:
                        raise WeatherError('invalid_response', '天气响应超过256KiB限制。')
                    raw.extend(chunk)
        try:
            return json.loads(raw)
        except (ValueError, UnicodeError):
            raise WeatherError('invalid_response', '天气源响应格式无效。') from None
    except httpx.TimeoutException:
        raise WeatherError('timeout', '天气查询超时。') from None
    except httpx.HTTPError:
        raise WeatherError('unavailable', '天气源连接失败。') from None


def render_weather(data, name, day, fetched_at, hit):
    def show(value, unit=''):
        return '未知' if value is None else f'{value:g}{unit}'
    def condition(code):
        return CODES.get(code, '未知')
    current, daily = data['current'], data['daily'][day]
    lines = [f'[天气资料] 地点：{name}（管理员配置；时间均为北京时间）',
             f'查询时间：{fetched_at}；当前数据有效时间：{data["current_time"]}']
    if hit:
        lines.append('缓存命中，复用原查询（最多5分钟）。')
    if day == 0:
        lines += [f'当前模型天气：{condition(current["weather_code"])}；气温{show(current["temperature_2m"], "℃")}；'
                  f'体感{show(current["apparent_temperature"], "℃")}；湿度{show(current["relative_humidity_2m"], "%")}；'
                  f'风速{show(current["wind_speed_10m"], "km/h")}。']
    lines += [f'{daily["date"]}日预报：{condition(daily["weather_code"])}；'
              f'最低{show(daily["temperature_2m_min"], "℃")}／最高{show(daily["temperature_2m_max"], "℃")}；'
              f'降水概率最大值{show(daily["precipitation_probability_max"], "%")}；'
              f'全天累计降水量{show(daily["precipitation_sum"], "mm")}。',
              '日预报不是此刻实况，降水概率不表示必然下雨。',
              f'来源：Open-Meteo {SOURCE} · CC BY 4.0；天气模型数据，非气象站实测。']
    return '\n'.join(lines)


class WeatherService:
    def __init__(self, *, fetch=fetch_weather, clock=time.monotonic,
                 now=lambda: datetime.now(TZ)):
        self.fetch, self.clock, self.now = fetch, clock, now
        self.lock = asyncio.Lock()
        self.cache = None
        self.failure = None

    async def query(self, config, day, client_options):
        if type(day) is not int or not 0 <= day <= 2:
            raise WeatherError('invalid_request', 'day只接受0今天、1明天、2后天。')
        name, lat, lon = location(config)
        now = self.now()
        key = (name, lat, lon, now.date().isoformat(), tuple(sorted(client_options.items())))
        def cached():
            record = self.cache
            if record and record['key'] == key and 0 <= self.clock() - record['at'] < CACHE_SECONDS:
                valid_at = local_time(record['data']['current_time'])
                if -600 <= (now - valid_at).total_seconds() <= 7200:
                    return record
            return None
        record = cached()
        hit = record is not None
        if record is None:
            if self.failure and self.failure['key'] == key and self.clock() < self.failure['until']:
                raise WeatherError('backoff', '天气查询暂处退避期，未再次请求；请稍后按需查询。')
            if self.lock.locked():
                raise WeatherError('busy', '已有天气查询在进行，未重复请求。')
            async with self.lock:
                try:
                    payload = await self.fetch(lat, lon, client_options)
                    finished = self.now()
                    data = parse_weather(payload, lat, lon, finished)
                    if finished.date() != now.date():
                        raise WeatherError('stale', '查询期间日期变化，请稍后按需重新查询。')
                    record = {'key':key, 'at':self.clock(), 'fetched_at':finished.isoformat(), 'data':data}
                    self.cache, self.failure = record, None
                except WeatherError as error:
                    self.failure = {'key':key, 'until':self.clock()+error.cooldown}
                    raise
        result = deepcopy(record['data'])
        content = render_weather(result, name, day, record['fetched_at'], hit)
        partial = any(value is None for key, value in result['daily'][day].items() if key != 'date')
        if day == 0:
            partial = partial or any(value is None for value in result['current'].values())
        return {'success':True, 'status':'partial' if partial else 'ok', 'content':content, 'source':SOURCE,
                'cache_hit':hit, 'fetched_at':record['fetched_at'], 'forecast_date':result['daily'][day]['date']}
