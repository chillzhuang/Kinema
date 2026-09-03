# 参考片读片 study 护栏

**实现真源** `kinema/study.py` ｜ **性质** 版权护栏

## 1. 三条护栏，一条都不能松

### 1.1 契约里的路径一律工作区相对

写成 `study/<slug>/ref.mp4`，**不许绝对路径**。

`storage/media.collect_media` 的收录规则是「`/` 开头 ＋ 媒体后缀 ＋ 在工作区内 ＋ 文件存在」。一旦
写成绝对路径，`oss sync` 就会把**第三方参考片传上用户自己的公网 OSS 并生成可访问 URL**，等同公网转载。

守卫用例 `TestRelativePathOnly` 用「相对不收 / 绝对必收」对照钉死。

### 1.2 产物目录不带 `_work` 后缀

用 `project/<pid>/study/<slug>/`。

`scanner.rglob("*_work")` 是 Studio 片库文件扫描的唯一入口——带后缀的参考片会被当成自家成片收进片库。

### 1.3 v1 只吃本地文件

不吃 URL、不引 yt-dlp。引擎核心不联网抓第三方内容是既有事实边界（`pyproject.dependencies = []`）。
用户要读某链接的片子，让他自己下载后给本地路径。

## 2. 另两条纪律

### 2.1 引擎只出可测量量

切点密度 / 每镜时长 / 静音占比 / 关键帧。

「该用 kenburns 还是 dubbed」属**判定**，写在 `kinema-project` SKILL「3.7 参考片立项模式」，由指挥层做
——引擎内无 LLM 是铁律。

### 2.2 切点全表进 sidecar

切点全表落 `digest.json`，契约只留指针 ＋ 计数（同 `source/segments.json`）。巨 blob 进 `project.json`
会拖垮每次 `Series.load`。

## 3. 参考片的去向禁区

- **绝不进 `exports/` 交付目录**；
- **绝不进任何生成请求**——它不是垫图；
- 读完即 `study rm`。
