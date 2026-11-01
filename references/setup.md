# Agent自动初始化

本文件供Agent执行。使用者只需表达“自动配置”，不需要安装Python、打开终端、复制草稿路径或查找依赖技能。复制技能目录不是安装钩子；首次调用时检查，得到授权后执行安装。

## 选择组件与授权

| features | 使用场景 | 自动配置范围 |
| --- | --- | --- |
| write | 写稿、核史、选题、计数 | 需要脚本时配置Python；文字创作不依赖媒体工具 |
| storyboard | 纯文本分镜与提示词 | 同上；口播时间对齐另加asr |
| images | 实际生图 | 复用宿主工具；按需安装可选imagegen指导，单独验证图像工具权限 |
| tts | 豆包MP3配音 | Python、websockets==15.0.1、FFmpeg/ffprobe；用户的Key、音色和模型资源另按[配音配置](doubao-voice.md)引导 |
| asr | 最终配音转录 | Python、Node.js LTS及npm、Coli、FFmpeg与ffprobe |
| draft | 已有素材生成剪映草稿 | Python、pyJianYingDraft==0.3.1、Pillow、NumPy、原生libmediainfo、FFmpeg与ffprobe、草稿目录 |

“一键配置全部功能”选draft,asr,images,tts；只生成已有素材草稿选draft，需要转录再加asr。只写稿不安装剪映、Node或FFmpeg。初始化只安装软件依赖，不获取Key、不开通付费资源，不生成配音、图片或实际工程。

先运行check。用户已要求初始化、一键配置、自动安装，或本会话已授权相同依赖时，直接install。普通创作请求首次遇到缺依赖时，结合check结果一次展示组件与用途，提供“自动配置并继续（推荐） / 暂不配置”。获得选择后Agent全程执行，不反复确认每个包。初始化全部功能且Windows缺剪映时，将剪映桌面应用一起列入该次选择；已有剪映不重装。只要生成工程文件、明确不要安装应用时不加安装剪映选项。

## 原生入口：电脑没有Python也可启动

Agent根据操作系统运行以下命令，替换为当前安装的实际绝对路径。命令供Agent使用，不作为交给用户的待办。check不下载、不写配置；没有Python时返回初步安装计划，不能把初步计划当作全部已实测。

Windows（自带Windows PowerShell）：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "<技能绝对路径>\scripts\bootstrap.ps1" -Action check -Features "draft,asr,images,tts" -InstallJianying
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "<技能绝对路径>\scripts\bootstrap.ps1" -Action install -Features "draft,asr,images,tts" -InstallJianying
```

macOS/Linux：

```bash
bash "<技能绝对路径>/scripts/bootstrap.sh" check --features draft,asr,images,tts
bash "<技能绝对路径>/scripts/bootstrap.sh" install --features draft,asr,images,tts
```

按需调整features；Windows的-WithImagegen、其他系统的--with-imagegen只安装缺少的可选指导技能。imagegen固定到脚本中的完整Git提交SHA；有Git时复用Git，没有Git使用GitHub官方HTTPS归档；保留上游许可，不覆盖已有技能。安装说明不会授予生图能力或付费额度。宿主缺少图像工具时Agent先检查可用连接或插件，只在需要账户授权时请用户完成授权，不收集或写入密钥。

## 安装行为

1. 复用Python3.10+，推荐3.11；缺失时从Astral官方uv发布下载工具并核验SHA-256，用uv安装用户专用Python3.11。安装文件和Python位于当前用户数据目录，不改全局PATH。
2. asr需要Node.js20+及npm；缺失时从Node.js官方下载LTS便携版本，核验官方SHA-256，再安装到专用目录。Coli及完整npm依赖树按随附package-lock.json安装到专用目录，执行npm ci并关闭生命周期脚本。Windows的npm、Coli通过原生Node与包内JS入口运行，参数不经过.cmd批处理。仅对当前子进程设置工具PATH。
3. Windows缺FFmpeg/ffprobe时使用FFmpeg官网列出的Gyan essentials构建，核验发布方SHA-256。媒体Python包按requirements-*.lock中的完整版本与wheel哈希安装，启用--require-hashes及--only-binary=:all:；缺包时安装到专用venv，已有解释器须通过锁定版本检查。Python探测、venv和pip子进程均使用-I；实际检查MediaInfo.can_parse()。
4. macOS复用Homebrew安装ffmpeg、libmediainfo；Debian/Ubuntu复用apt安装ffmpeg、libmediainfo0v5。没有包管理器或其他系统时由Agent选择官方安装方式并继续配置。系统权限不足时按宿主提供的授权流程申请；不要求用户自行运行sudo或安装命令。无法获得授权则保留已完成部分并准确说明阻塞，不宣称全部就绪。
5. Windows请求安装剪映时，已有应用直接复用；缺失则先用WinGet用户级静默安装。没有WinGet时读取Microsoft维护的剪映清单，下载厂商安装器并核验清单SHA-256，使用其声明的静默参数。安装后重新检测。安装器、网络或系统授权异常时Agent检查官方来源处理，不反复盲目重试。
6. 草稿目录依次使用显式参数、环境、运行配置、剪映globalSetting中的currentCustomDraftPath、已有标准目录。新Windows安装自动创建标准草稿目录；其他系统可保存工程到专用drafts目录。已有剪映设置仍无法读出时由Agent查看设置核对，避免用户手动找路径。只读取相关明文设置，不读取应用凭据或账户存储。
7. 完成后重跑同一原生入口check及setup_dependencies.py check。检查Python库版本、模块来源、API、libmediainfo、FFmpeg/ffprobe、Coli位置及所需应用。asr实际执行前核验Coli能运行；首次转录可下载模型，初始化不下载模型、不上传音频，不调用火山字幕API。

脚本是可重复入口，不是后台安装服务。遇到失败返回agent_action_required或error时，Agent查看当前失败项、修复可控问题后再运行；锁校验失败时保持隔离模式和哈希要求，核对具体包、平台与下载来源；依赖升级须重新生成并审查完整锁。不要把原始错误、私人路径或用户手动命令作为最终交付。网络下载与软件安装在用户已授权范围内执行；系统授权、账户登录和工具授权仍可能需要用户本人操作。

## 配置与验证范围

专用Python、venv、Node、FFmpeg、Coli、缓存和runtime.json保存在技能目录之外的用户数据目录story-studio中。Windows基于LOCALAPPDATA，其他系统基于XDG_DATA_HOME或用户数据目录约定。runtime.json仅记录python、coli_bin、drafts_root、tool_dirs；STORY_STUDIO_CONFIG可改配置位置，但禁止写进技能源码。

JY_PROJECTS_ROOT、COLI_BIN可覆盖对应工具与目录。导出、渲染和交接检查入口自动启动配置的Python并设置当前进程工具PATH；不修改用户或系统全局环境变量，不修改其他技能源码。

ready只说明所选依赖检查通过，不代表生图授权已获得、ASR模型已下载或草稿已在剪映验收。当前完整制作链以Windows桌面中国剪映为目标；macOS/Linux原生安装入口与生成文件能力不等于完整剪映桌面导出已验证。实际作品仍须检查素材、时间轴、关键帧和目标剪映中的预览。

tts依赖就绪也不代表豆包Key、额度和音色已可用；继续执行[语音配置引导](doubao-voice.md)，不能把安装成功当作一次收费API验证已通过。

来源：[uv管理Python](https://docs.astral.sh/uv/guides/install-python/)、[Node.js](https://nodejs.org/en/download)、[FFmpeg](https://ffmpeg.org/download.html)、[Gyan构建](https://www.gyan.dev/ffmpeg/builds/)、[WinGet清单](https://github.com/microsoft/winget-pkgs/tree/master/manifests/b/ByteDance/JianyingPro)、[pyJianYingDraft](https://pypi.org/project/pyJianYingDraft/0.3.1/)、[Coli](https://github.com/marswaveai/coli)、[imagegen](https://github.com/openai/skills/tree/main/skills/.system/imagegen)。
