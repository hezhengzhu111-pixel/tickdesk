# TickDesk Android App

TickDesk 用于核对工作台事项、记录实际成交、查看已确认持仓和与本地助手对话。原生 UI 以 [最新小米 17 Ultra 设计稿](../docs/app-ui/tickdesk_17_ultra_1.html) 为视觉基准。五个主 Tab 保留各自的滚动位置，底部显示待决策事项和有效持仓计数。主要页面的顶部栏在滚动时保留，成交、导入、对话和应用内强提醒提供固定操作区。系统状态栏、手势条与锁屏通知由 Android 呈现。

- 事项列表和提醒先打开事项详情，查看依据、来源与操作记录。详情提供关联回填、忽略和按规则稍后提醒。持仓页提供独立补录，成交页保留草稿并显示原请求的提交、确认与待对账状态。
- 选股展示扫描状态和候选结果，分析暂停时禁用全市场扫描。相机入口直接系统选图，上传、识别和结果留在当前页，确认后显示已刷新的持仓。持仓展示台账、可用数量、行情覆盖、估值曲线和手工更正。手动新增支持名称/代码 300ms 联想，选择真实证券后保存，等待服务确认和持仓刷新。证券搜索、关注和持仓均可进入 K 线。支持日线、周线和月线，以及不复权、前复权和历史翻页。行情卡显示参考价、报价时间口径、交易时段和供应可用性。
- 选股的「量化研究观察」独立展示已声明能力的 `get_quant_observations` 结果：策略/周期/研究阶段、K 线时间、入场与退出三态、分数及缺失原因。支持手动读取、冻结分页和新扫描结果到达后刷新；新扫描失败不会把旧研究当作新结果。研究卡仅用于读取，不创建操作卡、生产策略或持仓。
- 持仓页的「成交待对账」按标的汇总实际成交。按券商快照填写台账，勾选已包含的成交后等待工作台确认。版本变化时可在保留草稿的同时刷新并核对最新快照。
- 对话展示本机队列、分页历史、完整答复和来源。对话右上角的「分析报告」可筛选证券、查看当前或历史报告，核对覆盖、来源、证据时点与有效期，并选择按原报告或当前情况追问。设置提供扫码和配置文件导入、指纹核对、暂停、权限回读及恢复对账。恢复支持收集、上传、封存证据和核对非成交差异，服务确认后等待最新快照。
- 日常截图导入自动完成上传、授权确认、识别及保存，仅展示识别失败、内容不合法、成功三类终态。配对/纪元绑定的持久工作流可断线恢复；成功以 confirmed 和完整持仓快照为准。设置中的原图与高级导入管理支持 JPEG/PNG 预览、分块上传、断点续传、分页查看原图、下载校验、取消传输及手动删除。已有待核对批次时可用「跳到识别结果」定位编辑区。上传保存、识别告知、识别任务和台账差异分别等待服务确认。批次草稿在切换页面时保留，批次版本改变后重新核对。
- 提醒通过可信连接核验事项与分析报告。事项按夜间设置显示应用内严重提醒或 Android 通知。工作台发布的报告提醒在 get_report 核验后使用低优先级通知通道。点击通知会再次核验当前配对、纪元、对象状态和提醒有效期。“后台提醒”开关控制常驻 WebSocket 通知和连接。

业务页通过 ViewModel 读取 Repository 的已确认投影和本机意图。未配对可查看页面并编辑部分表单。正式 APK 中没有业务 fixtures 和伪造台账。应用使用随包提供的 v2 schema，无相邻工程源码依赖。

持久联调使用独立安装包 **TickDesk 联调**（`dev.tickdesk.app.demo`）：以 `-PtickdeskMockDemo=true` 构建，每页标记模拟数据，连接独立演示本地数据库。安装入口为仓库根 `scripts/mock-demo.ps1 -Action Install`；以后运行 `-Action Start` 恢复服务。模拟事项、持仓、对话与截图导入经过真实服务链路并保留。详见 [假数据联调说明](../docs/testing/mock-business-flows.md)。

独立 Gradle 工程包含纯 Kotlin `core` 和 Android `mobile`。当前为 Gradle 9.6.0、AGP 9.5.1、Kotlin 2.3.10、Compose BOM 2025.10.01，Android SDK 36。最低 Android 8.0（API 26），验证使用 JDK 21。使用 Room、KSP、OkHttp、ZXing 和 WorkManager。

```powershell
cd android-app
$env:ANDROID_HOME = "$env:LOCALAPPDATA\Android\Sdk"
.\gradlew.bat :core:test :mobile:assembleDebug :mobile:lintDebug
```

输出 `mobile/build/outputs/apk/debug/mobile-debug.apk`，使用调试签名。需要设备验证时，显式指定 ADB 序列号。以下测试可安装到已授权的测试目标。

真实研究读取使用已有配对，显式运行 `adb -s <serial> shell am instrument -w -e class dev.tickdesk.app.DeployedServiceTest -e quantResearchTest true dev.tickdesk.app.test/androidx.test.runner.AndroidJUnitRunner`。测试只读核验能力声明和分页，要求至少一条真实观察且至少一个退出布尔值已知；没有数据即失败，不注入 fixtures。`QuantResearchRenderTest` 单独验证研究卡三态与缺失/失败/加载/不支持文案，使用仅限测试的展示数据。

后台提醒偏好仅对当前配对有效，默认关闭；切换配对清除偏好，恢复核对会停止服务。Android 通知权限和后台连接通知通道必须允许。服务采用 Android 14+ `specialUse` 类型并在 manifest 说明用途，发布 Google Play 时须申报并接受审核。只从可见 App 启动或恢复，使用 `START_NOT_STICKY`；没有开机自启或后台拉起。设备重启、强制停止、系统回收后需再次打开 App。前台服务不能绕过 Doze、省电限制或网络中断，不保证每条提醒即时送达；重连仅核验当前尚有效的提醒，过期提醒不补响。官方依据：[服务类型](https://developer.android.com/develop/background-work/services/fgs/service-types#special-use)、[后台启动限制](https://developer.android.com/develop/background-work/services/fgs/restrictions-bg-start)、[通知权限](https://developer.android.com/develop/ui/compose/notifications/notification-permission)。

隔离构建保留 USB 调试 CA 配置，避免覆盖普通开发产物：

```powershell
.\gradlew.bat :core:test :mobile:assembleDebug :mobile:lintDebug :mobile:assembleDebugAndroidTest -PtickdeskUsbIntegration=true --project-cache-dir ../data/app-integration/android-gradle-cache
```

`BackgroundReminderServiceTest` 的真实启动/返回桌面/停止用例仅在显式传入 `-e backgroundReminderTest true` 时执行，需要设备已有配对及通知允许，测试结束恢复原后台提醒偏好；它不替换配对、不清除业务数据。权限拒绝用例使用包装 Context，不修改设备授权。真实设备执行命令：

```powershell
adb -s <serial> shell am instrument -w -e class dev.tickdesk.app.BackgroundReminderServiceTest -e backgroundReminderTest true dev.tickdesk.app.test/androidx.test.runner.AndroidJUnitRunner
```

```powershell
.\gradlew.bat :mobile:assembleDebugAndroidTest
adb -s <serial> install -r mobile/build/outputs/apk/debug/mobile-debug.apk
adb -s <serial> install -r mobile/build/outputs/apk/androidTest/debug/mobile-debug-androidTest.apk
adb -s <serial> shell am instrument -w dev.tickdesk.app.test/androidx.test.runner.AndroidJUnitRunner
python tools/ui_smoke.py --serial <serial> --keyboard
```

原图端到端检查需要 OpenSSL、已构建的相邻 relay 与本地服务调试二进制、已安装上述两个 APK 的模拟器。测试使用临时证书、临时配对和独立数据目录，运行结束清理临时进程与 ADB 转发。

```powershell
python tools/test_isolated_file_service.py --serial emulator-5566
```

实际联调需要管理员分发有效的 v2 App 配置。Android 校验证书链、IP SAN 和 SPKI pin。发布时核对随同版本导出的契约清单和实际服务能力。验证记录与未完成项见 [开发记录](docs/development.md)，跨端依赖见 [契约问题](docs/contract-issues.md)。
