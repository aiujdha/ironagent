# MySQL 8.4 镜像 glibc(Ksplice)整改建议 —— 豁免评估报告

| 项 | 内容 |
|---|---|
| 日期 | 2026-06-03 |
| 评估对象 | 容器镜像 `ironagent-mysql:8.4-fips`(基于官方 `mysql:8.4`,底层 Oracle Linux 9-slim) |
| 涉及包 | `glibc`、`glibc-common`、`glibc-minimal-langpack` |
| 扫描建议 | `2.34-231.0.1.el9_7.10` → `2.34-231.0.1.ksplice1.el9_7.10` |
| 评估结论 | **建议豁免**:该建议属 Oracle Ksplice 在线热补丁项,不适用于不可变容器;当前 glibc 已是 OL9 最新基线,无未修复的常规 CVE 敞口。 |
| 处置状态 | 待安全团队确认豁免 |

---

## 1. 结论先行

扫描器建议把 glibc 三件套从 `…el9_7.10` 升到 `…ksplice1.el9_7.10`。经核查,**这并非一个"常规安全补丁落后"的问题,而是建议叠加 Oracle Ksplice 在线热补丁**。基于以下三点,建议对容器镜像豁免此项:

1. **当前 glibc 已是 OL9 BaseOS 最新基线**(实测 `Nothing to do`),并非落后于任何已发布的常规修复;
2. **Ksplice 用户态包需 Oracle Premier/ULN 付费订阅**,不在公开仓库,构建链路无法合规获取;
3. **Ksplice 的"运行时热补丁"范式与不可变容器范式不匹配**,容器靠重建拉取最新基线即可获得等效修复。

豁免**不引入未修复的常规 glibc CVE 敞口**(见第 5 节)。

---

## 2. 扫描原始发现

| 包 | 当前版本 | 建议版本 |
|---|---|---|
| `glibc` | `2.34-231.0.1.el9_7.10` | `2.34-231.0.1.ksplice1.el9_7.10` |
| `glibc-common` | `2.34-231.0.1.el9_7.10` | `2.34-231.0.1.ksplice1.el9_7.10` |
| `glibc-minimal-langpack` | `2.34-231.0.1.el9_7.10` | `2.34-231.0.1.ksplice1.el9_7.10` |

来源:`/var/lib/rpm/rpmdb.sqlite`。

---

## 3. 关键定性:`ksplice1` 是 Ksplice 热补丁包,不是常规 CVE 补丁

### 3.1 版本号剖析

```
当前：  glibc  2.34-231.0.1.el9_7.10
建议：  glibc  2.34-231.0.1.ksplice1.el9_7.10
               └──────────┬──────────┘  └───┬───┘
            主体 NVR 完全一致           仅在 release 中插入 ksplice1
```

upstream 版本(`2.34`)、主 release(`231.0.1`)、发行标签(`el9_7.10`)**完全一致**,差异仅是中间插入了 `ksplice1` 标记。常规安全补丁的特征是主 release 号递增(如 `231.0.1` → `231.0.2`),此处并无递增。

### 3.2 `ksplice1` 的含义

`ksplice1` 是 **Oracle Ksplice** 的用户态热补丁包标记。Ksplice 是 Oracle 提供的**不停机在线打补丁**技术,在 Oracle Linux 上除内核外,也可对 `glibc`、`openssl` 等用户态库热打补丁——即在**不重启进程、不替换 rpm** 的前提下,把安全修复动态应用到正在运行的进程。

### 3.3 与本批 FIPS 整改的本质区别

同批扫描中的 openssl/openssl-libs/gnutls 建议的是 `_fips` 后缀(FIPS 140-3 验证构建,公开 validation 仓库可得,已落地);而 glibc 建议的是 `ksplice1`(订阅制热补丁)。**两者性质不同,不能同等处理**。

---

## 4. 豁免论据

### 4.1 当前 glibc 已是 OL9 BaseOS 最新基线（实测）

在镜像内向 OL9 BaseOS 刷新元数据后查询是否有更新的常规 glibc,结果为 `Nothing to do`:

```text
$ rpm -q glibc glibc-common glibc-minimal-langpack
glibc-2.34-231.0.1.el9_7.10.aarch64
glibc-common-2.34-231.0.1.el9_7.10.aarch64
glibc-minimal-langpack-2.34-231.0.1.el9_7.10.aarch64

$ microdnf --refresh --assumeno upgrade glibc glibc-common glibc-minimal-langpack
Downloading metadata...
Nothing to do.
```

含义:**当前镜像已包含 Oracle 通过正常 erratum 渠道发布的全部 glibc 安全修复**,并非"落后一个补丁"。扫描器也未指出存在更高版本号的常规 glibc——它建议的只是叠加 Ksplice 热补丁层。

### 4.2 Ksplice 用户态包需付费订阅,公开仓库不可得

Oracle Ksplice 的接入需通过 **Unbreakable Linux Network(ULN)** 配置增强客户端,属 **Oracle Linux Premier Support** 订阅范畴。`ksplice1` 标记的 glibc 包不在公开的 `yum.oracle.com` 仓库中,**当前构建链路无法在不引入付费订阅凭证的情况下合规获取该包**。

### 4.3 Ksplice 范式与不可变容器范式不匹配

- **Ksplice 的设计目标**:为**长生命周期、不能频繁重启**的主机/虚拟机,在两次正常更新之间提供不停机热补丁。
- **容器的运维模型**:镜像**不可变、短生命周期**,安全修复通过**重建镜像 + 拉取最新基线 rpm**获得,而非在容器内运行热补丁客户端。
- 在容器内常驻 Ksplice 客户端守护进程属于**反模式**:既违背不可变镜像原则,又增加攻击面与运维复杂度。

因此,对容器而言,"重建镜像取最新基线"是与 Ksplice **等效且更契合**的修复路径。

---

## 5. 风险评估:豁免不构成常规 CVE 敞口

| 维度 | 评估 |
|---|---|
| 常规 erratum 修复 | **已全部覆盖**。4.1 实测证明当前即 BaseOS 最新基线,无 pending 常规 glibc 修复。 |
| Ksplice 增量价值 | Ksplice 相对基线的增量,主要是"在某修复进入正式 erratum 之前抢先以热补丁形式提供"。对会**定期重建**的容器,该时间窗价值有限——容器通过下一次重建即可获得对应 erratum。 |
| 残余风险 | 若某 glibc 高危 CVE 的修复在某时间窗内**仅经 Ksplice 提供、尚未进入 BaseOS erratum**,则该窗口内容器不享有该热补丁。此风险通过第 6 节补偿控制收敛。 |
| 底层库替换风险(若强行执行) | glibc 是系统最底层 C 库,mysqld 及全部二进制均链接它;在容器内以非标准 `ksplice1` 包替换 glibc 存在导致 mysqld 无法启动的高风险。**豁免反而规避了这一稳定性风险。** |

**净结论**:豁免后,容器在常规安全修复维度与"已升级到最新 glibc"等价;Ksplice 带来的增量收益对不可变容器边际有限,且其落地伴随订阅依赖与稳定性风险。**风险可接受。**

---

## 6. 替代措施 / 补偿控制

为确保豁免后 glibc 持续处于安全基线,采取以下补偿控制:

1. **定期重建镜像**:跟随 OL9 BaseOS 的 glibc erratum 发布节奏重建 `ironagent-mysql:8.4-fips`,以拉取最新基线包。
2. **构建期基线校验(可固化进 CI)**:在镜像构建或 CI 中加入只读校验,确保 glibc 为当前 BaseOS 最新:
   ```bash
   microdnf --refresh --assumeno upgrade glibc glibc-common glibc-minimal-langpack
   # 期望输出：Nothing to do.（若列出可升级项，则触发重建）
   ```
3. **扫描策略校准**:对**容器镜像**的漏洞扫描,将 `ksplice*` 类建议归类为"主机热补丁项 / 容器场景不适用",与"基线 CVE 未修复"区分对待,避免噪声告警掩盖真实风险。

---

## 7. 请求处置

请安全团队对镜像 `ironagent-mysql:8.4-fips` 的下述 3 项标记为 **「豁免 —— 容器场景不适用」**,并记录本报告为依据:

- `glibc` → `2.34-231.0.1.ksplice1.el9_7.10`
- `glibc-common` → `2.34-231.0.1.ksplice1.el9_7.10`
- `glibc-minimal-langpack` → `2.34-231.0.1.ksplice1.el9_7.10`

**复评触发条件**(满足任一则重新评估):

1. OL9 BaseOS 发布更高版本号的常规 glibc(届时通过**重建镜像**修复,而非 Ksplice);
2. 出现**仅 Ksplice 覆盖、且无对应 BaseOS erratum** 的高危 glibc CVE;
3. 合规要求明确强制容器镜像启用 Ksplice / FIPS 运行模式。

---

## 附录:可复核命令

安全团队可自行复现本报告的实测证据:

```bash
# 启动镜像并查当前 glibc 与是否有 BaseOS 更新
docker run --rm --entrypoint bash ironagent-mysql:8.4-fips -c '
  rpm -q glibc glibc-common glibc-minimal-langpack
  microdnf --refresh --assumeno upgrade glibc glibc-common glibc-minimal-langpack
'
```

**参考资料**:Oracle Linux Ksplice User's Guide — <https://docs.oracle.com/en/operating-systems/oracle-linux/ksplice-user/>
