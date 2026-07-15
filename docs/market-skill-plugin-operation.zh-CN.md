# IronAgent Market Skill / Plugin 操作手册

说明：

- 这份文档是当前版本的持续维护文档。
- 只要 `Market` 页面或 `Assistants` 页面的 `skill/plugin` 装配、停用、启用、复用流程发生变化，这份文档都需要同步更新。

这份手册专门说明 `Admin -> Market` 页面的使用方法，以及 `skill/plugin` 和“助手、工作空间、容器”之间到底是什么关系。

如果你现在最关心的是下面这些问题，这份文档就是给你的：

1. 上传的 `skill/plugin` 会保存到哪里
2. 下载按钮是下载到本地，还是下载到容器
3. 直接装配到某个助手是什么意思
4. 助手怎么查看自己的已装 `skill/plugin`
5. 助手怎么停用、启用、复用它们
6. 安装后的文件会落到哪里
7. 怎么验证它真的已经进入助手容器可用了

## 1. 先记住三个动作

`Market` 里最重要的是三个动作：

1. `Upload`
2. `Download archive`
3. `Install to assistant`

它们不是一回事。

### 1.1 Upload

作用：

把一个 `skill/plugin` 原始压缩包上传到 IronAgent 的内部市场。

上传后，包会进入全局市场仓库，而不是直接进入某个助手。

### 1.2 Download archive

作用：

把市场里的原始归档包下载到你当前打开浏览器的这台机器。

它是“导出原包”，不是“装进助手”。

### 1.3 Install to assistant

作用：

把市场里的某个 `skill/plugin` 安装到指定助手的工作空间里。

这个动作才会让它和某个助手真正绑定。

补充说明：

- 安装完成后，如果你要立刻通过该助手的 `gateway_url` 进 IronAgent 页面验证效果，宿主机上的 `127.0.0.1` 和 `localhost` 需要走直连
- 如果本机开了全局代理，像 `http://127.0.0.1:55202` 这种助手本地网关地址可能会出现假性 `502`
- 命令行验证时，优先使用：

```bash
curl --noproxy '*' -I http://127.0.0.1:<gateway_port>
```

## 2. skill / plugin 和助手是怎么对应的

对应关系不是靠一张单独的“安装关系表”，而是靠“助手自己的工作空间目录”。

逻辑是：

1. 每个助手都有自己的 `workspace_root`
2. `Install to assistant` 时，你在页面里选择一个助手
3. IronAgent 会把包解压到这个助手自己的工作空间里
4. 包会被复制到这个助手自己的 `skills/` 或 `plugins/` 目录
5. 该助手容器会挂载这个工作空间
6. 所以这个助手在容器里就能看到安装进去的 `skills/plugins`

也就是说：

`assistant_id -> workspace_root -> skills/plugins -> 对应助手容器可见`

## 3. 文件到底落在哪里

要分成“全局市场层”和“助手工作空间层”。

### 3.1 全局市场层

上传成功后，原始包会落到：

```text
data/market/catalog.json
data/market/packages/skills/<record_id>/<archive>.zip
data/market/packages/plugins/<record_id>/<archive>.zip
```

这部分是全局的。

含义是：

1. 所有助手共享同一个市场
2. 这里保存的是“原始上传包”
3. `Download archive` 下载的也是这里的内容

### 3.2 助手工作空间层

安装成功后，文件会落到某个助手自己的工作空间：

```text
<workspace_root>/<workspace_slug>/skills/<skill_slug>/
<workspace_root>/<workspace_slug>/plugins/<plugin_slug>/
```

这部分是和助手绑定的。

含义是：

1. A 助手安装了，不代表 B 助手也安装了
2. 同一个市场包可以安装到多个助手
3. 每个助手工作空间里是一份自己的安装副本
4. 如果你没有自定义工作空间，默认通常会落到：

```text
data/workspaces/<assistant_id>/...
```

5. 如果你配置了自定义 `workspace root` 或 `workspace slug`，实际路径以后台返回的 `install_path` 为准

### 3.3 容器里看到的路径

助手容器里会把这个工作空间挂载到：

```text
/home/node/.ironagent/workspace
```

所以在容器里实际看到的是：

```text
/home/node/.ironagent/workspace/skills/<skill_slug>/
/home/node/.ironagent/workspace/plugins/<plugin_slug>/
```

## 4. 页面操作步骤

### 4.1 上传 skill

进入 `Admin -> Market -> Skills`。

准备一个 zip 包，推荐最少包含：

1. `SKILL.md`
2. 如果有的话，也可以带 `manifest.json`

然后填写：

1. `Slug`
2. `Display Name`
3. `Owner`
4. `Version`
5. `Tags`
6. 选择 zip 包

点击：

`Upload`

成功后，你会在列表里看到一条新的 skill 记录。

### 4.2 上传 plugin

进入 `Admin -> Market -> Plugins`。

准备一个 zip 包，推荐最少包含：

1. `manifest.json`
2. 插件主文件，例如 `plugin.py`

然后同样点击：

`Upload`

成功后，你会看到一条新的 plugin 记录。

### 4.3 下载原始归档包

在 `Skills` 或 `Plugins` 页签里：

1. 先选中一条记录
2. 如果归档在本地存储，页面下方会显示 `Download archive`
3. 如果归档在远端存储，页面下方会显示 `Open download URL`
4. 点击后，浏览器会把原始归档包下载到本地，或跳转到远端下载地址

这里请注意：

这是“下载到你本地机器”，不是下载到助手容器。

### 4.4 直接安装到某个助手

在 `Market` 页面中：

1. 选中一个 `skill` 或 `plugin`
2. 在 `Install to assistant` 中选择目标助手
3. 点击 `Install to assistant`

安装成功后，页面会显示：

1. 安装目标助手 ID
2. 安装路径
3. `install_count`

这里要注意：

1. 这是“直接装配到助手”
2. 不是先下载到容器里再手工拷贝
3. 页面会把包直接铺到目标助手的工作空间

### 4.5 在助手页面查看自己的 skill / plugin 列表

进入 `Admin -> Assistants`，先选中一个现有助手。

页面中会有一个区块：

`Installed skills and plugins`

这里会展示这个助手自己的安装列表，按两类分开：

1. `Installed Skills`
2. `Installed Plugins`

每条记录会显示：

1. `slug`
2. `name`
3. `version`
4. 当前状态 `enabled/disabled`
5. 实际安装路径

### 4.6 停用和启用

在 `Admin -> Assistants -> Installed skills and plugins` 中：

1. 选中一个已装的 `skill` 或 `plugin`
2. 点击 `Disable item`
3. 需要恢复时，再点击 `Enable item`

当前版本的实现方式是：

1. 启用状态的项目位于：

```text
<workspace_root>/<workspace_slug>/skills/<slug>/
<workspace_root>/<workspace_slug>/plugins/<slug>/
```

2. 停用状态的项目会被移动到：

```text
<workspace_root>/<workspace_slug>/.ironagent-disabled/skills/<slug>/
<workspace_root>/<workspace_slug>/.ironagent-disabled/plugins/<slug>/
```

这样做的意义是：

1. 助手还保留这份包
2. 但 IronAgent 正常工作区下不再直接看到它
3. 重新启用时不需要重新从市场安装

### 4.7 复用到另一个助手

如果某个助手已经装好了一个 `skill` 或 `plugin`，你可以在 `Assistants` 页面里直接把它复用到另一个助手。

操作步骤：

1. 在 `Installed Skills` 或 `Installed Plugins` 中选中一个条目
2. 在 `Reuse to assistant` 中选择目标助手
3. 点击 `Reuse to selected assistant`

复用后的行为是：

1. 会把当前助手工作空间里的那份安装副本复制到目标助手
2. 目标助手会得到一份新的启用副本
3. 如果目标助手之前已有同名启用或停用副本，会被新的副本替换

所以“复用”更准确地说是：

从某个助手复制一个已装包到另一个助手。

## 5. 怎么验证安装真的成功

最简单的验证方式有三层。

### 5.1 看后台返回

点击 `Install to assistant` 后，页面会提示类似：

```text
Installed into `.../<workspace_slug>/skills/<skill_slug>`. Count: 1.
```

这说明已经落到了目标助手的工作空间。

### 5.2 看宿主机目录

在 `ironagent` 仓库执行：

```bash
find <workspace_root>/<workspace_slug>/skills -maxdepth 3 -type f | sort
find <workspace_root>/<workspace_slug>/plugins -maxdepth 3 -type f | sort
```

如果安装成功，你会看到类似：

```text
<workspace_root>/<workspace_slug>/skills/<skill_slug>/SKILL.md
<workspace_root>/<workspace_slug>/skills/<skill_slug>/scripts/...
<workspace_root>/<workspace_slug>/plugins/<plugin_slug>/manifest.json
<workspace_root>/<workspace_slug>/plugins/<plugin_slug>/plugin.py
```

### 5.3 看容器内路径

如果要确认助手容器是否真的能看到，可以执行：

```bash
docker exec ironagent-assistant-<assistant_id> sh -lc '
find /home/node/.ironagent/workspace/skills -maxdepth 3 -type f | sort
find /home/node/.ironagent/workspace/plugins -maxdepth 3 -type f | sort
'
```

如果这里也能看到，就说明容器内已经可用了。

## 6. 一个最常见误区

很多人会把“下载”和“安装”混在一起。

你可以直接按下面这个表理解：

| 动作 | 去向 | 作用 |
| --- | --- | --- |
| Upload | `data/market/...` | 进入全局市场 |
| Download archive | 你当前浏览器所在本机 | 导出原始包 |
| Install to assistant | `<workspace_root>/<workspace_slug>/...` | 安装到指定助手 |

所以：

1. `下载` 不是给助手用的
2. `安装` 才是给助手用的
3. 助手容器真正使用的是它自己工作空间里的安装副本

## 7. office skill / plugin 的推荐验收方式

如果你要验证 office 相关能力，推荐这样做：

### 7.1 office skill

推荐用：

`ironagent-main/skills/office-docs`

把它打成 zip 后上传到 `Market -> Skills`。

安装到某个助手后，可以在容器内执行：

```bash
docker exec ironagent-assistant-<assistant_id> python3 \
  /home/node/.ironagent/workspace/skills/<skill_slug>/scripts/make_office_docs.py \
  --out-dir /home/node/.ironagent/workspace/temp/office-smoke \
  --title 'Office Smoke' \
  --body 'Generated by market-installed office skill.' \
  --stdout-json \
  --sheet-name 'Summary' \
  --headers 'Item,Status' \
  --row 'Office Skill,Installed'
```

期望生成：

1. `agent-word.docx`
2. `agent-word.pdf`
3. `agent-sheet.xlsx`
4. `agent-sheet.pdf`

### 7.2 office plugin

如果你现在验证的是“市场上传、下载、安装、容器内可访问”，那么最小插件包里有：

1. `manifest.json`
2. `plugin.py`

就够了。

安装后可以在容器里做一个最小导入验证：

```bash
docker exec ironagent-assistant-<assistant_id> python3 -c "
import importlib.util, pathlib
path = pathlib.Path('/home/node/.ironagent/workspace/plugins/<plugin_slug>/plugin.py')
spec = importlib.util.spec_from_file_location('plugin_mod', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
print(module)
"
```

## 8. 删除助手后会怎样

如果你删除一个助手：

1. 这个助手对应的容器会被清理
2. 这个助手的工作空间会被清理
3. 这个助手工作空间中的已安装 `skills/plugins` 也会一起消失

但是：

全局市场里的原始归档包仍然还在。

也就是说：

```text
data/market/...                     还在
<workspace_root>/<workspace_slug>/ 对应助手那份没了
```

这也符合设计预期：

市场是“包仓库”，助手工作空间是“安装副本”。

## 9. 你平时最常用的检查命令

查看市场原始包：

```bash
find data/market -maxdepth 4 -type f | sort
```

查看某个助手已安装的 skill：

```bash
find <workspace_root>/<workspace_slug>/skills -maxdepth 3 -type f | sort
```

查看某个助手已安装的 plugin：

```bash
find <workspace_root>/<workspace_slug>/plugins -maxdepth 3 -type f | sort
```

查看某个助手已停用的 skill / plugin：

```bash
find <workspace_root>/<workspace_slug>/.ironagent-disabled -maxdepth 4 -type f | sort
```

查看某个助手容器里是否可见：

```bash
docker exec ironagent-assistant-<assistant_id> sh -lc '
ls -la /home/node/.ironagent/workspace
'
```

## 10. 推荐你怎么用

日常最推荐的顺序是：

1. 先 `Upload`
2. 再点一次 `Download archive`
   - 确认原始包没有问题
3. 再 `Approve`
4. 然后 `Install to assistant`
5. 去 `Assistants` 页面确认已装列表里能看到它
6. 如有需要，再做 `Disable / Enable / Reuse`
7. 如果要继续用该助手的 `gateway_url` 做 UI 验证，先确认浏览器访问本地地址不走代理
8. 最后去助手容器里做一次最小 smoke 验证

这样最稳，也最容易定位问题。
