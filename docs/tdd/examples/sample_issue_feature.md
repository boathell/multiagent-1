# 示例：新增用户列表分页参数校验（Feature）

## 1. 背景与目标
- 背景：用户列表接口在非法分页参数下返回 500，缺少输入校验。
- 目标：为 `page`、`page_size` 增加参数校验，非法值返回 400。

## 2. 范围 / 非目标
- 范围：接口层参数校验、错误码与错误信息、测试补齐。
- 非目标：不修改数据库 schema，不改分页查询策略。

## 3. Red 阶段（先失败）
| 测试ID | 文件 | 用例名 | 预期失败原因 | 执行命令 | 证据占位 |
|---|---|---|---|---|---|
| RED-API-001 | tests/test_users_api.py | test_list_users_invalid_page | 目前未校验 page，返回 500 | uv run pytest -q tests/test_users_api.py::test_list_users_invalid_page | 待补日志 |
| RED-API-002 | tests/test_users_api.py | test_list_users_invalid_page_size | 目前未校验 page_size，返回 500 | uv run pytest -q tests/test_users_api.py::test_list_users_invalid_page_size | 待补日志 |

## 4. Green 阶段（最小实现）
| 模块 | 最小改动 | 关联测试ID |
|---|---|---|
| src/api/users.py | 增加 `page>=1`、`1<=page_size<=100` 校验，失败返回 400 | RED-API-001, RED-API-002 |
| src/api/errors.py | 复用现有 BadRequest 错误结构 | RED-API-001, RED-API-002 |

## 5. Refactor 阶段（重构）
| 目标 | 风险 | 回归命令 |
|---|---|---|
| 抽取分页参数校验函数，减少重复逻辑 | 可能影响其他接口入参解析 | uv run pytest -q |

## 6. 验收标准（DoD）
- [ ] Red 用例先失败后通过
- [ ] 非法分页参数返回 400
- [ ] 原有用户列表 happy path 不回归

## 7. 风险与回滚
- 风险：历史客户端依赖非法参数被默认纠正。
- 回滚：回退至前一版本并移除校验逻辑。

