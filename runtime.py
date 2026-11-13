"""Request deadlines, bounded text and tracked lifecycle work."""
import asyncio
import time
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import wraps


FOLLOWUP_SCOPE_HINT = '后续调用若需session_id，沿用本次真实目标会话，勿编造或填写_host_context。'


def response_status(text):
    first_line = text.split('\n', 1)[0]
    match = re.match(r'^(?:Exa 搜索|Jina SERP \((?:google|bing)\) 搜索|Firecrawl 原文|Jina 原文|AnySearch |搜索|原文)?状态：([a-z_]+)', first_line)
    if match:
        return match.group(1)
    if '未启用' in first_line:
        return 'disabled'
    return 'invalid_request' if any(x in first_line for x in ('失败', '无法', '没有当前聊天')) else 'ok'


def tracked(function):
    @wraps(function)
    async def call(self, *args, **kwargs):
        async with self._work.request():
            result = await function(self, *args, **kwargs)
        if isinstance(result, str):
            code = response_status(result)
            success = code in {'ok', 'partial', 'no_results'}
            return {'success': success, 'status': code, 'content': result}
        return result
    return call


def scoped(function):
    """Reject missing Host identity before cache access or external work."""
    @wraps(function)
    async def call(self, *args, **kwargs):
        disabled = self._check_enabled()
        if disabled:
            return {'success': False, 'status': 'disabled', 'content': disabled}
        if not self._text_scope(kwargs):
            return {'success': False, 'status': 'invalid_context',
                    'content': '可信宿主会话身份缺失或矛盾，未读取缓存、联网或发送。请管理员核查会话路由；不要自填身份或_host_context。'}
        return await function(self, *args, **kwargs)
    return call


def bounded_text(text: str, limit: int, notice: str = '\n[内容已截短]') -> str:
    if len(text) <= limit:
        return text
    return text[:max(0, limit - len(notice))] + notice[:limit]


@dataclass(frozen=True)
class Deadline:
    expires: float

    @classmethod
    def after(cls, seconds: float):
        return cls(time.monotonic() + seconds)

    def remaining(self, reserve: float = 0) -> float:
        return max(0, self.expires - time.monotonic() - reserve)


class WorkManager:
    def __init__(self):
        self.tasks = set()
        self.ready = True

    @asynccontextmanager
    async def request(self):
        if not self.ready:
            raise RuntimeError('插件正在重载')
        task = asyncio.current_task()
        self.tasks.add(task)
        try:
            yield
        finally:
            self.tasks.discard(task)

    async def close(self):
        self.ready = False
        tasks = [task for task in self.tasks if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
