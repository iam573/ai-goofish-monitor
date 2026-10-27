# 闲鱼 iOS 多开签名实验

把原始闲鱼 IPA 生成为多个可并存安装的副本：

- 桌面名称始终是“闲鱼”。
- 图标右上角显示 `2`、`3`、`4`……。
- 包名依次是 `com.efrenwu.goofish2`、`com.efrenwu.goofish3`、`com.efrenwu.goofish4`……。
- 按 `.local.env` 中的证书匹配值和 Personal Team 签名，不会自动选择其他证书。
- 保留已验证的网络兼容层，同时避免 UIKit/CoreUI 取到伪装包名导致底部图标残缺。
- 移除原 IPA 的 App Extensions，避免免费账号为每个副本额外消耗多个 App ID 和描述文件。

## 使用

生成并安装编号 2：

```bash
./clone-goofish.sh --install 2
```

一次生成编号 2、3、4（不安装）：

```bash
./clone-goofish.sh 2 3 4
```

指定 IPA、输出目录或设备：

```bash
./clone-goofish.sh \
  --ipa /absolute/path/to/闲鱼iOS.ipa \
  --output-dir /absolute/path/to/output \
  --device YOUR_DEVICE_UDID \
  --install \
  2 3
```

默认输出到原 IPA 旁边的 `clones/`。首次运行会在系统临时目录创建 Python 虚拟环境，并安装 LIEF 和 Pillow。

本机专用的 IPA 路径、证书匹配值、Team ID 和设备 UDID 放在被 Git 忽略的 `.local.env` 中；复制到别的 Mac 时可参考 `.local.env.example` 修改。

Apple 已实际拒绝 Personal Team 注册 `com.taobao.fleamarket2`，提示该标识不可用；因此它不能作为可签名安装的副本包名。需要改用其他可注册前缀时可传 `--bundle-prefix`，编号仍会自动追加：

```bash
./clone-goofish.sh --bundle-prefix com.example.goofish 2 3
```

## 限制

免费 Personal Team 的描述文件通常约 7 天有效，而且 Apple 会限制一台设备上同时安装的免费签名 App 数量。脚本可以连续生成多个 IPA，但超过账号或设备限额时，Xcode 申请描述文件或设备安装会失败；这不是脚本能绕过的限制。

本实验会改变包名并注入兼容动态库。它只处理用户提供的 IPA，不保存 Cookie、Token、WUA、登录状态或 App 容器数据。
