# Neko Web

1.11.0：模型工具由10个合并为7个：统一单次/批量搜索、网页/长文读取、图片初次预览/翻页。网页默认只取文字，配图须显式选择；正文续读、原图发送、天气和专用目录仍独立。供应商/密钥、授权、并发/额度和后台签名API不变，不因合并启用automation。

给 [MaiBot](https://github.com/Mai-with-u/MaiBot) 用的联网插件。搜索公开网页，读取标题、正文和候选图片，由麦麦看图挑选后随回复发送。

搜索走 [AnySearch](https://anysearch.com)。打开链接和下载图片由插件自己完成。搜索客户端的早期实现来自 [gomico/maibot_plugin_anysearch](https://github.com/gomico/maibot_plugin_anysearch)，该项目以 MIT 许可证发布。

1.3.0可在 `[web]` 设置 `search_provider="exa"`、`exa_api_key`，普通/批量搜索使用Exa auto＋有来源的摘录，专用领域继续AnySearch。设置 `extract_provider="firecrawl"`、`firecrawl_api_key` 后长文工具走Firecrawl v2 Markdown提取（含动态页），`read`和图片仍走原本公网下载器。默认仍为AnySearch；不自动跨供应商重试、不开deep/crawl/付费代理等额外能力。两家均按服务商额度计费，请自行设额度限制。

Firecrawl提取会把目标URL交给第三方。发送前验证URL及DNS为公网，但远端DNS/重定向由Firecrawl处理，不能宣称具有本地下载器同等IP固定保护；不要用于敏感或带访问令牌的链接。响应限2MiB，首次正文展示按配置限长，错误不回显原始响应或密钥。

## 安装

把这个目录放到 MaiBot 的 `plugins/neko-web`，复制 `config.example.toml` 为 `config.toml`，然后在 WebUI 里启用。

`api_key` 可以留空，匿名访问的额度更低。需要代理时，把 `proxy_mode` 改成 `custom`，并填写 `proxy_url`。不要把填了密钥的 `config.toml` 提交到仓库。

依赖 `httpx >= 0.26.0`、`aiohttp >= 3.12`、`Pillow >= 12.0`。MaiBot 1.3.5 及以上，SDK 2.7 及以上；直接看图需要启用 Planner 视觉并配置支持图片的模型。

## 工具

### 统一入口（1.11.0）

- `neko_web_search(queries=["一个问题"])`或最多5个问题，先去重，整批仍最多20条/2并发。`max_results`指每个问题；仅单问题支持原有`read_pages=0..2`和专用数据源参数，多问题不能静默忽略这些参数或自动取原文。普通搜索无需domains。
- `neko_web_read(url, mode="page", include_images=false)`默认只从公开链接读标题/正文，留存供text续读，不创建图片队列、不下载或解码候选图；图片直链会提示用预览工具，不把图片当正文。`include_images=true`才沿用原配图预览流程，不直接发送。
- `neko_web_read(url, mode="article")`显式走已配置AnySearch/Firecrawl/Jina长文服务，可能计费，不执行本地失败后自动升级/重试或换源。article只取正文，不能同时设include_images=true；返回的配图链接可另用images。
- `neko_web_images(urls=["https://..."])`首次预览，`neko_web_images(cursor="上次next_cursor")`继续；二选一，不能同时传。原聊天隔离、一次游标、10分钟时效、图片预算与仅预览语义保持。

旧`neko_web_batch_search/neko_web_extract/neko_web_images_next`不再注册给模型；底层旧处理方法仅保内部调用兼容，不是另外三份对外工具。下方旧版本小节的旧工具名属于历史，当前调用以本节及工具表为准。模型发现描述保持短小，正文/图片下一步参数由实际结果按需提示，不新增常驻提示或修改人设。

### 按需天气（1.10.0）

`neko_web_weather(day=0)`：0为今天（含当前模型天气）、1明天、2后天。只有按需工具调用才请求，不在启动、定时任务或每轮主动聊天中自动查询，也不会自行发QQ/调用模型。天气工具只读管理员配置的一个公共地点，不接收模型给出的城市、URL或经纬度，不按IP猜位置；其他地点需普通搜索，不能把配置地点当所有群友的位置。

WebUI“按需天气”提供独立enabled开关、location_name名称、WGS84 latitude纬度/longitude经度；北纬/东经为正，名称仅用于显示，不自动地理编码。新安装默认关闭、地点空白，先由管理员填写坐标并授权启用；实例真实配置不入Git。时间固定北京时间，温度℃、风速km/h、累计降水mm，日预报降水概率不是确定降雨。

- 固定`https://api.open-meteo.com/v1/forecast`，无新Key，不发送用户账号/聊天/人设或现有搜索密钥；只发送配置坐标及字段/单位。沿用web代理选择但独立匿名HTTP会话，无自动重定向、重试或跨源回退，最大256KiB，整轮10秒/单请求8秒。配置变更或撤销后旧结果不提供，卸载取消在途任务。
- 当前值来自天气模型，不冒充气象站实测、分钟级降雨、空气质量或官方预警；按有效时间、时区/日期/坐标/单位核验，过旧/日期不符/畸形响应明确失败，缺失可选值显示未知。提供Open-Meteo来源/CC BY 4.0及查询/数据有效时间，模型回答应保留来源和时间，不编造亲历。
- 成功数据仅内存缓存5分钟，今天/明后天复用同份3天资料；缓存不后台刷新。失败短退避，429尊重有界Retry-After，正在查询时拒绝重复请求；不把旧缓存冒充新天气，不触发普通搜索兜底。
- [Open-Meteo使用条款](https://open-meteo.com/en/terms)：免费端点仅非商业使用，少于10000次/日，并有小时/分钟频率限制；商业用途需自行取得合适服务授权，不承诺永久免费或持续可用。[数据许可](https://open-meteo.com/en/licence)为CC BY 4.0；服务可能记录请求IP及坐标（其条款说明日志保留90天），因此只填获准发送的公共地点坐标，不填敏感精确住址。插件MIT不替代数据归属要求。

### 可选 Jina SERP 手动搜索（1.9.0）

`web.manual_search_provider="configured"`默认沿用原`search_provider`。显式改为`"jina_serp"`后，仅普通/批量**手动工具**搜索用Jina SERP；`jina_search_engine="google"`（默认）或`"bing"`选择引擎，认证复用`jina_api_key`。专用领域仍AnySearch、Life后台仍使用原`search_provider`，不让手动选源顺带改变自主探索。

- 固定`GET https://serp.jina.ai/`，只传实测可用的`q/engine/num`，只取标题、URL、snippet和可选日期，不默认抓正文。2026-10-20托管API实际为`{results,meta}`，`provider=bing`被忽略并返回google，`type=web`被400拒绝，与Reader仓库旧API文档不一致；因此不盲传旧参数、不兼容猜测未知响应结构。核对`meta.engine/meta.query`及逐项引擎，不符就明确失败，不静默接受、改写或跨源重试。
- 返回结果复用URL去重、来源多样性、限长及缺失日期标记，不把摘要当事实或全文；返回引擎核对不保证供应商内部没有缓存/回退，新鲜度及实际收费仍未知。暂不支持sites/published_after/published_before，传入会在搜索前明确拒绝，不忽略或擅自拼进关键词。原read_pages=0–2仍是显式额外读正文，按已选extract后端执行/留存续读；局部失败保留搜索证据。
- 新增`jina_proxy_url`：仅Jina Search/Reader可选专用HTTP(S)/SOCKS代理，留空沿用原代理。其他Exa/Firecrawl/AnySearch/Life配置不受影响；不得把Jina代理配置转发成供应商付费代理参数。新安装与现有部署不自动填代理、不切手动搜索来源；超时/并发/总期限/2MiB响应/未知不重放保留，无额外模型工具。
- 小样本（3个公开问题、每家前3条、非统计性排名）：Google与Exa能找到技术/项目官方来源；中文航天样本Exa更贴题，Bing返回无关理财顾问结果。Jina返回的credits在本轮Google每次1、Bing每次2，只是供应商字段，不等于精确金额或永久价格。初步建议保留Exa默认、Google按需补充，Bing暂不推荐作为默认，不能声称全网比较胜出。
- 已用服务器内配置Key经隔离代理真实验证两引擎和Reader，未把Key/配置写进Git或公开报告。原直连超时与匿名401记录仍有效，不宣称直连已恢复。线上仍保留原Exa＋Firecrawl，开启前需显式选手动来源及可用Jina网络路径。

配置示例（自行填真实Key，代理按实际网络，不照抄他人地址）：

```toml
[web]
search_provider = "exa"               # 原来源，Life后台保持使用
manual_search_provider = "jina_serp"  # 仅手动普通/批量覆盖
jina_search_engine = "google"
jina_api_key = "YOUR_JINA_KEY"
jina_proxy_url = ""                  # 留空沿用原代理；可单独指定Jina代理
```

### 可选 Jina Reader（1.8.0）

在`[web]`显式设置`extract_provider="jina"`后，`neko_web_extract`及Exa搜索的`read_pages`使用Jina Reader；`jina_api_key`可留空尝试匿名，或填写自己的Jina Key。仅填Key不会切源，失败不会自动换Firecrawl/匿名重试，也不增加模型工具或每次调用的供应商覆盖参数。新安装默认仍AnySearch，既有Firecrawl配置保持。**本机当前仍使用Firecrawl，未启用Jina。**

- 固定`POST https://r.jina.ai/`，目标URL放JSON正文，保留查询参数与SPA片段；请求Markdown + JSON响应，要求Jina断言目标HTTP状态为200，不把目标错误页面主动作为正文。422可能是目标状态断言或请求参数拒绝，不能简单等同404。严格检查响应envelope、字段类型及非空正文；错误只给安全类别，不泄露密钥、原始错误或正文。
- URL/DNS发出前校验公网，返回来源发生变化时再次校验；Jina自己执行DNS/重定向，不具有本地下载器的固定IP保证，事后校验不能阻止供应商已访问的重定向。目标URL会交给第三方，不传私密、带登录令牌或一次性授权链接；不转发群聊、Cookie、宿主密钥或完整人设。未承诺供应商零保留。
- 沿用代理选择、超时/槽位/2MiB流式响应限额，正文留存128000字符/聊天隔离/固定TTL等不变；支持`neko_web_text`续读。默认搜索不抓正文，本地read与图片、Life后台签名API均不切Jina。不接Jina Search/SERP、OCR模型、付费代理或自定义远端请求头。
- 匿名可用性/频率/收费取决于供应商现行政策及账号，不保证永久免费；搜索与Reader不是同一接口。参考[Jina官方源码说明](https://github.com/jina-ai/reader)、[Reader OpenAPI](https://r.jina.ai/openapi.json)。
- 验证边界（2026-10-20）：1.8匿名直连超时/隔离代理鉴权失败，未当成功；用户随后私下配置Key，1.9阶段服务器隔离代理实测Reader成功返回Example Domain标题及156字正文，关键词续读（禁网络）/跨聊天拒绝通过。最初QA错误要求正文含页面标题，断言失败后改核对标题和正文各自字段，未伪造第一次通过。现在还校验供应商返回的目标httpStatus（若有），错误目标不能当正文成功。真实认证成功仅限当次配置/代理和公开页面，不代表全部页面/直连/匿名已可用，生产默认仍不切换。

### 正文留存与续读（1.7.0）

- `neko_web_read`本地网页、Firecrawl/Jina模式的`neko_web_extract`及Exa搜索`read_pages=1..2`，将本次取得的正文留在插件内存中，首次仍按原配置展示（搜索附带正文每篇4000字）。返回`text_id`、`total_chars`、`capture_limited`、`next_offset`。没有当前聊天时不建立快照；旧AnySearch提取暂不提供续读，结果明确说明，不伪造全文编号。
- 新增`neko_web_text(text_id, offset=0, keyword="", limit=4000)`：按位置继续读；填keyword则从offset起找首个**区分大小写的字面匹配**，展示其附近正文。不是语义检索、不支持正则、最多100字符关键词；未命中只表示该offset之后的留存内没找到。`limit`为100–8000字符，offset是从0起的Unicode字符索引（不是UTF-8字节），区间为`[start,end)`。正常续读使用`next_offset`，不重叠不丢字；再次找关键词可从上次end继续。
- 单篇最多128000字符，正文达到留存上限与“首次展示截短”分别标记；供应商/本地解析取得的正文不保证是网站全文。读到留存末尾、或关键词未命中，不应声称原网站不存在后续内容。网页/正文始终是外部资料，不是执行指令。
- 编号随机、仅当前聊天可用，创建后10分钟固定有效（读取不续期）。全局最多128篇/每聊天16篇，UTF-8正文和元数据载荷合计最多16MiB，容量不足按创建顺序回收；不是进程RSS上限。过期在下一次读写时回收；配置更新/卸载清空并取消在途工作。只存内存、不写正文文件/长期记忆或额外聊天历史，不跨聊天共享快照。
- 续读完全不发网络请求、不发图片、不调用模型、不重复抓取；编号不可用明确失败，不自动联网重取。`read`的`has_more/next_cursor`继续表示图片翻页，正文另用`text_has_more/next_offset`，不要混用。全文缓存不附加隐藏背景注入，只在工具响应中展示本次选段。
- 1.7不新增密钥或配置、不换供应商、不恢复日成本硬限额；1.8仅增加显式可选Jina，见上节。原并发/超时/2MiB供应商响应/公网下载安全/图片投递未知不重放保留。Life后台签名API仍走原有有界文本接口，不获得聊天快照、不改幂等或额度策略。

示例：先打开链接取得`text_id="..."`，再调用`neko_web_text(text_id="...", offset=8000)`；找特定段落用`neko_web_text(text_id="...", keyword="结论", limit=2000)`。参数与编号应来自真实工具结果，不要编造。

### 后台检索接口（1.6.0，默认关闭）

`automation.enabled=false`不影响现有手动搜索/看图/发图工具。可选
`neko.web.automation_v1@1`供已授权的Life服务提交、查询和取消有界后台任务，
不增加模型工具数量，也不在RPC内等待长搜索。此接口按MaiBot1.3.9/SDK2.12验证。

宿主不传递可信调用者身份给接收器，因此必须单独在本插件data_dir配置0600的
`automation-keys.json`（客户端ID到随机能力密钥），再显式开启接口。
不要把供应商Key当能力密钥，不要把该JSON入Git。启动不自动生成或发放密钥。
每个客户端HMAC签名绑定操作、参数、受众、时间与nonce；nonce和幂等任务ID持久化，
结果/取消按已鉴权客户端隔离。密钥不能提供同服务账号程序之间的OS级隔离。

后台并发1、总队列4、完整结果最多128份；每客户端每日搜索/读网页默认各6次（Asia/Shanghai），
1.6.1可用`automation.daily_search_max/daily_read_max`分别配置，0=不限；
失败/取消/未知不退外部请求预算。无自动供应商切换或重试。后台与手动请求共享原总槽位，
后台最多占一个槽，不因后台用尽预算禁止手动搜索。结果满时回收至少180秒前的终态载荷，保留最多8192条/7天幂等墓碑，不因结果过期重复外部请求；重载将未完任务
收口为unknown，不自动重放。每个结果≤30000 UTF-8字节，不携带图片或视频二进制。

Exa复用手动工具的结构化来源整理；AnySearch只接受已支持的results JSON，
未知展示文本返回unsupported，不解析自然语言猜成功。后台read明确使用原公网下载器
读取HTML/纯文本/JSON，最多2MiB/4000字，不执行JS、不取图片；它不是Firecrawl长文提取，
不会为后台阅读自动切到另一个付费服务。来源缺发布日期就保留未知，用量和金额未知不填0。

已通过一轮真实Exa搜索及2篇NASA公开正文读取，Life成功保存来源笔记。自动化测试仍使用模拟网络和SDK，不证明全部AnySearch格式、动态网页或供应商永久可用。

### 搜索增强（1.4.0）

- Exa结果按规范化URL去重：忽略锚点和常见追踪参数，保留有意义的查询参数。不同页面不强行合并；同站结果分层交错，减少单站挤占，不把来源多样性当权威评分。保留标题、真实来源、日期和有界摘录，日期缺失明确标记，不猜“最新”。不会自动追加搜索补满去重后的数量。
- `neko_web_search` 和 `neko_web_batch_search` 可选 `sites`（最多5个域名）、`published_after` / `published_before`（YYYY-MM-DD，含当天，UTC）。只用于Exa普通搜索；AnySearch或专用领域遇到这些参数明确返回unsupported，不静默忽略。发布日期是供应商元数据，不是插件核实过的更新时间。
- 单条搜索可选 `read_pages=0..2`，默认0。开启后读取整理后前1–2个来源的Firecrawl正文，每篇最多4000字；这是额外请求、额外额度，不保证是模型已判定最相关的文章。读原文失败/超时保留搜索证据并标partial；不配置Firecrawl时不发起正文请求。批量搜索不自动读正文，需要时另选来源提取。
- 状态区分ok/no_results/partial、密钥权限、额度限流、参数、超时和供应商异常；无结果不代表事实不存在，摘录不代表已读原文。不自动换源、重搜或改写关键词。
- 普通和批量搜索共用2个并发槽；批量最多5个去重后的问题、整批最多20条（按问题均分每条返回数量），总输出24000字并公平保留每个问题。单条输出16000字、整轮含排队最多95秒，批量总期限最多95秒；取消/超时会清理任务并保留已完成查询。AnySearch原始返回文本不强行假设其结构或伪造来源日期；结构化来源去重适用于Exa。

示例：`neko_web_search(queries=["MaiBot release notes"], sites=["github.com"], published_after="2026-10-01", read_pages=1)`。默认仍仅搜索，不触发原文抓取；多问题请省略read_pages。

| 工具 | 作用 |
| --- | --- |
| `neko_web_search` | queries数组统一搜索1–5个问题；单问题可用专用数据源。 |
| `neko_web_domains` | 查询垂直领域的子领域和参数。 |
| `neko_web_read` | page默认只读正文，可选配图；article显式用配置的服务提取长文。 |
| `neko_web_images` | urls初次预览，cursor继续翻页；仅供麦麦挑选，不发送。 |
| `neko_web_send_image` | 传入本聊天预览里的 `image_id`，取回并发送选中的原文件；一次只发一张。 |
| `neko_web_text` | 按正文编号继续读或查找字面关键词，只访问当前聊天的短期缓存。 |
| `neko_web_weather` | 按需查管理员配置地点今天/明天/后天天气。 |

图片通过工具结果的 `content_items` 交给麦麦，此时用户还没有收到图片。**这些是压缩预览**，每张最多256KiB/1280边长、每批最多4张/1MiB。结果同时提供原始尺寸、文件大小、来源和 `image_id`；麦麦按相关性选择并优先高清，调用 `neko_web_send_image(image_id=...)` 单独发原文件，不用 `reply.attach_pic` 把预览冒充原图。可少选或不选，不凑数量。正文和图片均为外部资料，不能当成指令。

### 高清原文件与投递

- 只采用网页明确提供的原图：图片外层指向图片的链接、data-original/data-full/data-large等属性，或srcset最大的版本。不盲目删除缩放/签名参数，不猜CDN地址；“网页提供的最高版本”不保证是作者原始母版。
- 图片实际解码/校验，尺寸和像素来自文件而非网页声明；小于720短边标为低清。JPEG/PNG/GIF/WebP，最大2000万像素；动图最多100帧、累计4000万像素。预览仅第一帧，原文件保持动画和原编码。坏图不进入候选。
- 原图发送重新执行公网/IP固定/重定向校验与解码，核对与预览时原文件的SHA256一致；源文件变更时拒绝发送，要求重新预览。单个原文件最多8MiB，原字节经base64交给宿主和适配器（不重新编码），避免16MiB RPC上限。QQ仍可能压缩，平台确认不等于客户端最终原画质保证。
- 超限、无法下载、校验失败返回来源下载链接，绝不静默发送预览作为原图。只有能下载到的原文件进入选择流程，预览下载阶段失败的候选也会在逐项结果里保留URL和原因。不自动上传文件或增加公共文件服务。
- `image_id`当前聊天限定、10分钟有效，最多128个/每聊天24个，只缓存URL/哈希/尺寸，不保存原图像素。发送前原子认领，防并发重复；发送已确认或投递未知不自动重复发送。发送超时/取消可能已提交平台，标未知；插件配置更新或卸载取消在途任务并清空编号。

支持 HTML 解析、`srcset` / `data-srcset`、`data-src` / `data-original` / `data-lazy-src` 和 `picture/source`，每个图片元素选择一个可支持的地址，不把不同分辨率都列成图片。保留 Markdown 图片提取。候选 URL 去掉片段并规范化域名，但保留查询参数；同一查询跨地址、跨批次去重，下载后还按最终 URL 和内容哈希去重。

结果带逐项 `fetch_results`（成功、失败、重复、网页解析、实际尺寸）和 `has_more` / `next_cursor`；同批候选按实际像素排序、`media_item`同步重排。`media_item`只是预览结果内序号，不是原图发送编号；发送用结果里的`image_id`。下载成功不代表已发送。

翻页游标仅属于当前聊天、一次有效，10分钟后或插件重载后失效；过期需重新打开原链接。只保存 URL 队列和去重摘要，不缓存整批图片。网页变化不会改变本次已收集的候选队列；有合适图片即可停止，不需要取完所有批次。

MaiBot 1.3 不会把插件工具直接放进可调用列表。规划器先看到「名字：描述」，再通过 `tool_search` 按名字和描述检索。因此工具名用 `neko_web_` 当命名空间即可，用途写在中文描述里。不要把工具名改成中文。

1.2.1起工具描述仅保留用途和选择入口。专用数据源要求写在搜索参数里，发图说明只在有候选图的结果里出现，翻页说明只在还有候选时出现；游标时效等由代码校验和失败结果处理，不反复塞入每个工具说明。普通搜索不必先查询domains。

## 限制

- 只接受 `http` 和 `https` 的 80 / 443 端口。
- 拒绝内网、本机、云元数据、带账号密码的地址，以及跳到这些地址的重定向。
- 请求前解析并固定公网 IP；代理 CONNECT 也使用该 IP，保留原域名的 Host、TLS SNI 和证书校验。解析失败就拒绝，不让代理再次解析目标域名。公网下载支持 HTTP/HTTPS 代理，SOCKS 仅用于搜索接口；使用 mihomo 时填写 HTTP mixed-port。
- 页面中损坏的图片 URL 会跳过，不影响正文或其他图片。
- 图片只收 JPEG、PNG、GIF、WebP。默认每批最多4张，单原文件8MiB；预览每张256KiB、整批1MiB，并非每次都应发送4张。
- 每网页最多收集48个候选，每次查询最多192项；达到上限会提示。每批最多尝试12个地址、图片下载阶段最多45秒，失败多时也能继续下一批。最多保留64个队列，每聊天4个；超出淘汰最早队列。继续预览仍逐次校验公网地址和重定向，不放宽下载限制。
- 返回给模型的正文默认不超过 8000 字。
- 不执行页面脚本，也读不了需要登录的内容。

## 开发

日常源码CI用Linux生产Python3.14.4/SDK2.16.0与Windows最低Python3.12/SDK2.12两组，保留完整自有用例、依赖/lint和最终包真实SDK导入；原8组兼容矩阵保留在v标签、手动full及有源码变化的夜间验证，另保留固定生产组合。`scripts/ci.py`要求真实SDK和关键HTTP/桥/路由/媒体/打包测试组实际执行成功，不以固定总数证明正确，仍拒绝除原Windows POSIX权限项以外的意外skip。这不是QQ或供应商实测，无真实Key/配置/DB/pycache，无自动Release或部署。纯根目录/docs下Markdown只查文档，不获得源码发布资格；上传仍需同main提交、核心测试/包验证及ci-gate/publish-gate成功，受控手动同HEAD核心验证可用于文档HEAD发版。Actions原固定SHA和14天artifact保留不变，不改变1.9.0运行逻辑。

1.5.0审查修复：AnySearch也使用2MiB流式响应上限、MCP isError识别和安全错误码，提取前URL/DNS公网校验；Firecrawl检查目标statusCode，Exa无效条目不伪装成空结果。工具统一返回success/status/content，局部失败保留证据；不回显供应商原始错误。正文输出预留已请求原文预算，来源和摘录限量，长URL导致少展示来源时明确标partial且不抓不会展示的来源。read优先main/article、去导航/页脚，配图失败保留正文。整轮deadline包含排队；公开读取/原图共享2个并发槽，图片队列在途任务计入容量，热更新/卸载取消在途任务和清理元数据。RPC预算测试、SDK真实导入/执行测试覆盖，而非AST抽取业务方法。第三方远程提取的DNS/重定向安全仍由服务商负责，与本地固定IP下载边界不同。

`public_http.py` 与 `neko-draw` 保持相同副本，独立安装时不互相依赖。修复下载安全逻辑时同步两份，并运行公网下载回归测试。

```bash
uv run --no-project --with maibot-plugin-sdk==2.12.0 --with httpx --with aiohttp --with pillow --with tzdata python -m unittest -q
```
