"""Real isolated Host Runner and weather tool, synthetic HTTP boundary only."""
import argparse
import asyncio
from contextlib import ExitStack
import importlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock


async def verify(host, report):
    source=Path(__file__).resolve().parents[1]
    with ExitStack() as cleanup:
        instance=Path(cleanup.enter_context(tempfile.TemporaryDirectory(prefix='neko-weather-qa-'))).resolve()
        cleanup.callback(os.chdir,Path.cwd())
        os.chdir(instance)
        os.environ.update(MAIBOT_INSTANCE_DIR=str(instance),MAIBOT_LOCALE='zh-CN',MAIBOT_HOST_VERSION='1.3.15')
        sys.path.insert(0,str(host))
        from src.plugin_runtime.runner.runner_main import PluginRunner,PluginActivationStatus
        from src.plugin_runtime.runner import runner_main
        from src.plugin_runtime.protocol.envelope import Envelope
        from src.common.database.database import engine
        from src.common.logger import close_handlers
        cleanup.callback(engine.dispose)
        cleanup.callback(close_handlers)
        assert runner_main._PROJECT_ROOT.resolve()==instance
        target=instance/'plugins/neko-web'
        target.mkdir(parents=True)
        for path in source.glob('*.py'):
            if not path.name.startswith('test_'):shutil.copyfile(path,target/path.name)
        shutil.copyfile(source/'_manifest.json',target/'_manifest.json')
        (target/'config.toml').write_text('[plugin]\nenabled=true\nconfig_version="1.0.0"\n[weather]\nenabled=true\nlocation_name="synthetic"\nlatitude=10\nlongitude=20\n',encoding='utf-8')
        runner=PluginRunner(host_address='unix:///unused-weather.sock',session_token='synthetic',plugin_dirs=[str(target.parent)])
        async def rpc(method,**kwargs):
            assert method in {'plugin.bootstrap','plugin.register_components','plugin.unregister'},'Forbidden live capability: '+method
            return SimpleNamespace(error=None,payload={'accepted':True})
        runner._rpc_client.send_request=AsyncMock(side_effect=rpc)
        metas=runner._loader.discover_and_load([str(target.parent)],extra_available={})
        assert len(metas)==1 and not runner._loader.failed_plugins
        meta=metas[0]
        assert runner._prepare_plugin_activation(meta) is None and not meta.config_error
        assert await runner._activate_plugin(meta)==PluginActivationStatus.LOADED
        try:
            module=importlib.import_module(type(meta.instance).__module__.rsplit('.',1)[0]+'.weather')
            now=module.datetime.now(module.TZ)
            payload={'latitude':10,'longitude':20,'timezone':'Asia/Shanghai','utc_offset_seconds':28800,
                'current_units':{k:module.UNITS[k] for k in module.CURRENT},
                'daily_units':{k:module.UNITS[k] for k in module.DAILY},
                'current':{'time':now.strftime('%Y-%m-%dT%H:%M'),'temperature_2m':0,'apparent_temperature':-1,
                    'relative_humidity_2m':50,'weather_code':0,'wind_speed_10m':5},
                'daily':{'time':[(now.date()+module.timedelta(days=i)).isoformat() for i in range(3)],
                    'temperature_2m_max':[10,10,10],'temperature_2m_min':[0,0,0],
                    'weather_code':[0,3,61],'precipitation_probability_max':[0,20,40],'precipitation_sum':[0,1,2]}}
            fetch=meta.instance._weather.fetch=AsyncMock(return_value=payload)
            for index,day in enumerate([0,1]):
                answer=await runner._handle_invoke(Envelope(request_id=index+1,message_type='request',
                    method='plugin.invoke_tool',plugin_id='neko.web',
                    payload={'component_name':'neko_web_weather','args':{'day':day}}))
                assert answer.error is None,answer.error
                value=answer.payload['result']
                assert value['success'] and value['cache_hit']==bool(index),value
                assert 'CC BY 4.0' in value['content'] and '非IP定位' in value['content']
            fetch.assert_awaited_once()
            result={'real_host_runner_sdk_weather_tool':True,'synthetic_source':True,
                'source_and_valid_time_present':True,'cache_reused':True,
                'real_weather_model_qq_calls':0,'new_install_weather_default_enabled':meta.instance.config_model().weather.enabled}
        finally:
            await runner._invoke_plugin_on_unload(meta)
    report.write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host-source',type=Path,required=True)
    parser.add_argument('--report',type=Path,required=True)
    args=parser.parse_args()
    asyncio.run(verify(args.host_source.resolve(),args.report.resolve()))
