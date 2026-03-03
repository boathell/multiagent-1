# 合并设计文档：SupaLearn 代码优化

**Issue ID:** af38ca19-4a04-4da9-97c8-9a06f9bf7b1b  
**Project ID:** 5316280a-ada0-44af-8c7d-3b1408e796a0  
**Repository:** https://github.com/boathell/multiagent-1.git  
**Target Path:** /Volumes/exFAT/supabase-web

---

## 1. 合并来源文档

| 文档 | Issue ID | 主要内容 |
|------|----------|----------|
| design-0838d4e2.md | 0838d4e2-d07b-4b99-8d81-47cb0ab9395b | 代码审查：性能、内存泄漏、可访问性 |
| design-d9d4a050.md | d9d4a050-1abb-44b3-81ea-004743ab3add | 代码审查：重复代码、构建配置 |
| design-7f0ff42a.md | 7f0ff42a-e95f-43ea-963e-1bcfa4dfaaed | 超链接替换 kimi*.com → 站内锚点 |

---

## 2. 问题现状分析

### 2.1 已修复问题 ✓
- kimi*.com 链接：当前代码中已无此类链接

### 2.2 待修复问题 ⚠️

| 优先级 | 问题 | 位置 | 影响 |
|--------|------|------|------|
| P0 | `kimi-plugin-inspect-react` 遗留于生产配置 | vite.config.ts | 构建产物包含开发插件 |
| P0 | Stats AnimatedCounter 使用 setState 驱动动画 | Stats.tsx | 频繁重渲染 |
| P1 | Hero canvas 无 aria-hidden | Hero.tsx | 可访问性 |
| P1 | scrollToSection 重复定义 | Navigation.tsx, Hero.tsx | 代码重复 |
| P1 | gsap.registerPlugin 多次调用 | Stats.tsx, Footer.tsx | 冗余注册 |
| P1 | canvas 无 visibilitychange 暂停 | Hero.tsx | 性能 |

---

## 3. 实现设计

### 3.1 模块结构

```
src/
├── utils/
│   └── scroll.ts          # 新增：统一滚动函数
├── hooks/
│   └── useReducedMotion.ts # 新增：检测减少动画偏好
├── sections/
│   ├── Hero.tsx           # 修改：canvas 优化、aria-hidden
│   ├── Stats.tsx          # 修改：Counter 优化
│   ├── Navigation.tsx     # 修改：使用统一 scroll 工具
│   ├── Footer.tsx         # 修改：移除冗余 gsap.registerPlugin
│   └── ...
└── vite.config.ts         # 修改：移除 kimi 插件
```

### 3.2 具体修改

#### Hero.tsx 优化
```typescript
// 1. 添加 aria-hidden 到 canvas
<canvas ref={canvasRef} aria-hidden="true" ... />

// 2. 添加 visibilitychange 处理
useEffect(() => {
  const handleVisibility = () => {
    if (document.hidden) {
      cancelAnimationFrame(animationId);
    } else {
      animate();
    }
  };
  document.addEventListener('visibilitychange', handleVisibility);
  return () => document.removeEventListener('visibilitychange', handleVisibility);
}, []);
```

#### Stats.tsx 优化
```typescript
// 使用 ref 直接操作 DOM，避免 setState
function AnimatedCounter({ value, suffix }: { value: number; suffix: string }) {
  const counterRef = useRef<HTMLSpanElement>(null);
  
  useEffect(() => {
    if (!counterRef.current) return;
    const obj = { val: 0 };
    gsap.to(obj, {
      val: value,
      duration: 1,
      ease: 'expo.out',
      onUpdate: () => {
        if (counterRef.current) {
          counterRef.current.textContent = Math.floor(obj.val) + suffix;
        }
      },
    });
  }, [value, suffix]);

  return <span ref={counterRef}>0{suffix}</span>;
}
```

#### vite.config.ts 优化
```typescript
// 移除 kimi-plugin-inspect-react
export default defineConfig({
  base: './',
  plugins: [react()], // 仅保留 react 插件
  resolve: { ... },
});
```

---

## 4. 验收标准 (Acceptance Criteria)

### 4.1 功能验收
- [ ] vite.config.ts 无 kimi 插件
- [ ] Hero canvas 有 aria-hidden="true"
- [ ] Stats 数字动画正常，无 setState
- [ ] 锚点跳转平滑

### 4.2 性能验收
- [ ] 页面隐藏时 canvas 动画暂停
- [ ] Stats 动画不触发 React 重渲染

### 4.3 代码质量
- [ ] npm run build 成功
- [ ] TypeScript 编译无错误
- [ ] 无 console.error/warning

---

## 5. 主要风险

| 风险 | 描述 | 缓解措施 |
|------|------|----------|
| R1 | Counter 优化后动画异常 | 添加 fallback，充分测试 |
| R2 | visibilitychange 逻辑错误 | 验证暂停和恢复逻辑 |

---

**整体评级:** B+ (良好，需性能优化)  
**核心建议:** 优先修复 vite 配置和 Stats 性能问题
