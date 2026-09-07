# 公司电脑本地网络配置工具

本仓库只包含公司网络覆盖层生成器、脱敏示例和测试，不包含个人代理节点、订阅 URL、
访问令牌或 Worker 配置。

## 获取工具

在公司电脑上让 Agent 克隆本仓库并进入目录：

```bash
git clone git@github.com:hex1n/company-overlay-kit.git
cd company-overlay-kit
python3 -m unittest scripts/test_configure_company.py
```

需要 Python 3.9 或更高版本。私有仓库需要该电脑已有可访问本仓库的 GitHub SSH
凭据；不要把个人 GitHub 凭据直接写进 Agent 提示或项目文件。

## 使用前

1. 确认公司政策允许在该电脑使用个人代理客户端和本工具。
2. 连接公司 Wi-Fi 或企业 VPN，并自行完成登录及 MFA。
3. 准备至少一个公司内部网站域名。不同网站的域名可分别填写，不需要共享后缀。
4. 不要向 Agent 或配置文件提供公司密码、证书、VPN Token 等凭据。

## 交给 Agent

在 `company-overlay-kit` 目录中启动 Agent，并提供以下指令：

```text
在当前 company-overlay-kit 目录中配置本机的公司网络覆盖层。我已经连接公司网络；
先只读检查 macOS 的 Split DNS 和路由，再向我确认缺少的内部域名或私网 IP。
除非检查结果证明必须使用固定私网 DNS，否则保持 DNS 为 system。
只写入 .local/company.local.json 和 .local/generated，运行生成器和全部测试，
并协助我把对应文件安装到桌面代理客户端。不要索取或保存 VPN 凭据。
```

Agent 不能凭空识别全部公司网站，也不能代替用户完成 VPN 登录或 MFA。至少提供一个
已知内部主机名，并确认需要访问的其他网站是否使用不同域名。

## 手动配置

先验证工具包：

```bash
python3 -m unittest scripts/test_configure_company.py
```

再运行交互式配置：

```bash
python3 scripts/configure_company.py
```

输入规则：

- 域名可以填写 `corp.example.com` 或 `*.corp.example.com`，两者都会匹配根域名
  及其全部子域名；多个不同域名用英文逗号分隔。
- 单个私网 IPv4 可以填写 `10.20.1.25`，会自动转换为 `/32`；网段可填写
  `10.20.0.0/16`。
- 不确定 DNS 时直接留空，使用 `system` 保留公司 Wi-Fi 或 VPN 的 Split DNS。
- Wi-Fi SSID 和 VPN 名称只用于生成操作说明，不会自动配置或登录企业 VPN。

生成文件位于 `.local/generated/`：

- `Surge-Company.sgmodule`
- `Shadowrocket-Company.module`
- `ClashVerge-Company.js`
- `INSTALL.txt`

只安装当前电脑代理客户端对应的文件，并按照 `INSTALL.txt` 完成启用和验证。连接
Split Tunnel 企业 VPN 时先连接 VPN，再使用系统代理，并关闭 Surge 增强模式或
Clash Verge Rev TUN。离开公司网络后停用公司模块或脚本。

## 验证 DNS 和路由

连接公司网络后执行：

```bash
scutil --dns
dscacheutil -q host -a name <公司内部主机名>
route -n get <公司内部IP>
```

公司域名应由系统解析为预期私网地址，路由应选择公司 Wi-Fi 或企业 VPN 接口，代理
客户端的规则测试结果应为 `DIRECT`。刷新个人代理订阅后重复测试，确认本地覆盖层
仍保持启用。
