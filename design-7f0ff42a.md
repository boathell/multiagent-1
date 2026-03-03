# 设计文档：修改网站超链接指向

**Issue ID:** 7f0ff42a-e95f-43ea-963e-1bcfa4dfaaed  
**Project ID:** 5316280a-ada0-44af-8c7d-3b1408e796a0  
**Title:** 修改网站超链接，使其指向自己而不是 kimi*.com  
**Repository:** https://github.com/boathell/multiagent-1.git  
**Target Path:** /Volumes/exFAT/supabase-web

---

## 1. 问题概述

### 1.1 背景
目标网站 `supabase-web` (SupaLearn) 是一个面向 DBA 的 Supabase 培训课程营销网站，使用 React + TypeScript + Vite 构建的单页应用(SPA)。

### 1.2 问题描述
网站中存在指向 `kimi*.com` 域名的超链接（如 `kimi.moonshot.cn`、`kimi.com` 等），需要将这些外部链接修改为指向网站自身的锚点链接（如 `#section-id`）或根路径（`/`）。

### 1.3 技术栈
| 层级 | 技术 |
|------|------|
| 框架 | React 19.2.0 + TypeScript 5.9.3 |
| 构建 | Vite 7.2.4 |
| 样式 | Tailwind CSS 3.4.19 + shadcn/ui |

---

## 2. 实现设计

### 2.1 链接识别范围

需要检查以下文件类型中的超链接：

```
supabase-web/
├── index.html                 # HTML 文件中的链接
├── src/
│   ├── sections/
│   │   ├── Navigation.tsx     # 导航链接
│   │   ├── Hero.tsx           # 首屏 CTA 链接
│   │   ├── LearningPath.tsx   # 学习路径链接
│   │   ├── CoreFeatures.tsx   # 功能展示链接
│   │   ├── CaseStudies.tsx    # 案例链接
│   │   ├── Resources.tsx      # 资源下载链接
│   │   ├── CTA.tsx            # 行动召唤链接
│   │   └── Footer.tsx         # 页脚链接（重点关注）
│   └── components/ui/         # UI 组件中的链接
```

### 2.2 链接修改策略

**需要修改的链接模式：**

| 原链接模式 | 目标链接 | 说明 |
|------------|----------|------|
| `https://kimi.moonshot.cn/*` | `#section-id` 或 `/` | 替换为站内锚点 |
| `https://kimi.com/*` | `#section-id` 或 `/` | 替换为站内锚点 |
| `http://kimi*.com/*` | `#section-id` 或 `/` | 替换为站内锚点 |

**推荐的锚点映射：**

```typescript
// 根据页面结构，建议使用以下锚点
const sectionIds = {
  hero: '#hero',              // 首屏
  learning: '#learning-path', // 学习路径
  features: '#features',      // 核心功能
  stats: '#stats',            // 数据统计
  cases: '#case-studies',     // 实战案例
  resources: '#resources',    // 资源
  cta: '#cta',                // 行动召唤
};
```

### 2.3 具体修改方案

**方案一：简单替换为根路径（推荐用于通用链接）**

```typescript
// 修改前
<a href="https://kimi.moonshot.cn/some-path">链接文本</a>

// 修改后
<a href="/">链接文本</a>
```

**方案二：替换为对应功能的锚点（推荐用于功能相关链接）**

```typescript
// 修改前
<a href="https://kimi.moonshot.cn/learn">开始学习</a>

// 修改后
<a href="#learning-path">开始学习</a>
```

**方案三：移除外部链接属性（`target="_blank"`、`rel="noopener noreferrer"`）**

当链接从外部改为内部时，应移除新窗口打开属性：

```typescript
// 修改前
<a 
  href="https://kimi.moonshot.cn/docs" 
  target="_blank" 
  rel="noopener noreferrer"
>
  文档
</a>

// 修改后
<a href="#resources">文档</a>
```

### 2.4 代码示例

以 Footer.tsx 为例的修改模式：

```typescript
// 修改前
const footerLinks = {
  resources: [
    { label: '官方文档', href: 'https://kimi.moonshot.cn/docs' },
    { label: '教程', href: 'https://kimi.com/tutorial' },
  ],
};

// 修改后
const footerLinks = {
  resources: [
    { label: '官方文档', href: '#resources' },
    { label: '教程', href: '#learning-path' },
  ],
};
```

---

## 3. 验收标准 (Acceptance Criteria)

### 3.1 功能性验收
- [ ] 所有指向 `kimi*.com` 域名的超链接已被识别并修改
- [ ] 修改后的链接指向有效的站内锚点（`#section-id`）或根路径（`/`）
- [ ] 站内锚点跳转平滑，能正确滚动到对应区域
- [ ] 修改后的链接不再包含 `target="_blank"` 和 `rel="noopener noreferrer"` 属性

### 3.2 代码质量验收
- [ ] `npm run lint` 无错误
- [ ] `npm run build` 构建成功
- [ ] TypeScript 编译无错误
- [ ] 无 console.error/warning

### 3.3 兼容性验收
- [ ] 锚点跳转在 Chrome/Firefox/Safari/Edge 最新版本正常
- [ ] 移动端锚点跳转正常
- [ ] 页面刷新后锚点定位正确

### 3.4 链接验证清单
- [ ] 检查 `index.html` 中的链接
- [ ] 检查 `Navigation.tsx` 中的链接
- [ ] 检查 `Hero.tsx` 中的 CTA 链接
- [ ] 检查 `Footer.tsx` 中的所有链接（包括 `footerLinks` 和 `socialLinks`）
- [ ] 检查 `Resources.tsx` 中的资源链接
- [ ] 检查 `CTA.tsx` 中的行动召唤链接

---

## 4. 主要风险 (Main Risks)

| 风险 ID | 风险描述 | 可能性 | 影响 | 缓解措施 |
|---------|----------|--------|------|----------|
| R1 | **遗漏链接** - 未完全扫描所有文件导致部分 kimi*.com 链接未被修改 | 中 | 高 | 1. 使用全局搜索（grep/ag）扫描所有源码文件<br>2. 检查 `.tsx`, `.ts`, `.jsx`, `.js`, `.html` 文件<br>3. 检查构建后的 `dist/` 目录确认无残留 |
| R2 | **锚点不存在** - 修改后的锚点指向不存在的 DOM id | 中 | 中 | 1. 确认所有锚点目标在对应组件中存在<br>2. 为缺少 id 的 section 添加 id 属性<br>3. 使用统一的 id 命名规范 |
| R3 | **平滑滚动失效** - 锚点跳转后没有平滑滚动效果 | 低 | 低 | 1. 确认 CSS `scroll-behavior: smooth` 已设置<br>2. 确认 `use-scroll-to-section` hook 正常工作<br>3. 测试各浏览器的滚动行为 |
| R4 | **构建产物残留** - 修改源码后 dist 目录仍有旧链接 | 低 | 低 | 1. 清理 dist 目录后重新构建<br>2. 在构建产物中搜索确认无 kimi*.com 链接 |
| R5 | **用户体验影响** - 原链接用户可能期望跳转到外部服务 | 低 | 低 | 1. 评估链接修改对用户预期的影响<br>2. 必要时添加提示说明链接已变更 |

---

## 5. 实施步骤

### 5.1 发现阶段
```bash
# 1. 搜索所有 kimi*.com 链接
grep -r "kimi" /Volumes/exFAT/supabase-web/src --include="*.tsx" --include="*.ts" --include="*.html"
grep -r "kimi" /Volumes/exFAT/supabase-web/index.html

# 2. 确认修改范围
# - 记录所有包含 kimi*.com 链接的文件和位置
```

### 5.2 修改阶段
1. 修改 `index.html` 中的链接（如有）
2. 修改 `src/sections/Navigation.tsx` 中的链接
3. 修改 `src/sections/Hero.tsx` 中的 CTA 链接
4. 修改 `src/sections/Footer.tsx` 中的 `footerLinks` 和 `socialLinks`
5. 修改 `src/sections/Resources.tsx` 中的资源链接
6. 修改 `src/sections/CTA.tsx` 中的行动召唤链接
7. 检查其他 section 文件中的链接

### 5.3 验证阶段
```bash
# 1. 构建项目
cd /Volumes/exFAT/supabase-web
npm run build

# 2. 验证构建产物中无 kimi*.com 链接
grep -r "kimi" dist/ || echo "✓ 无 kimi 链接残留"

# 3. 运行 lint
npm run lint

# 4. 测试锚点跳转
# - 启动开发服务器
# - 手动测试各修改后的链接跳转
```

---

## 6. 相关文件清单

| 优先级 | 文件路径 | 检查重点 |
|--------|----------|----------|
| P0 | `index.html` | `<a>` 标签 href |
| P0 | `src/sections/Footer.tsx` | `footerLinks`, `socialLinks` 数组 |
| P0 | `src/sections/Navigation.tsx` | 导航链接 href |
| P1 | `src/sections/Hero.tsx` | CTA 按钮链接 |
| P1 | `src/sections/CTA.tsx` | 行动召唤链接 |
| P1 | `src/sections/Resources.tsx` | 资源下载链接 |
| P2 | `src/sections/*.tsx` | 其他可能的链接 |

---

**文档生成时间:** 2026-03-03  
**审查范围:** /Volumes/exFAT/supabase-web/src
