# 山河漫游音乐素材谱系

报道描述年轻人假期奔赴山海、用车票串联沿途风光，国风与电子音乐结合并借助 AI 谱写中秋旋律，AI、文案与剪辑由不同参与者完成。本仓库记录素材来源、授权与发布谱系，支撑发行团队的合规审查。

## 目录

- `contracts/domain.schema.json`：领域事件交换契约（对象、事件与载荷约定）。
- `src/travel_music/contracts.py`：契约校验。
- `src/travel_music/models.py`：角色、状态与渠道策略模型。
- `src/travel_music/store.py`：SQLite 事件流与投影存储。
- `src/travel_music/service.py`：素材谱系服务（申报核验、签署、冻结、撤回、隔离、原子锁）。
- `src/travel_music/worker.py`：后台任务（下架期限与更正传播，支持中断恢复）。
- `examples/walkthrough.py`：端到端演示。
- `docs/domain.md`：领域对象与事件语义。
- `docs/service.md`：服务层业务规则。

## 测试

```bash
python3 -m unittest discover -s tests
```

## 编译检查

```bash
python3 -m compileall -q src tests examples
```

## 样例校验

```bash
PYTHONPATH=src python3 -m travel_music.cli contracts/domain.schema.json data/sample.json
```

样例有效时输出 `valid`；发现问题时逐行给出字段、代码与中文说明，并返回非零状态。

## 服务演示

```bash
PYTHONPATH=src python3 examples/walkthrough.py
```

演示渠道限制拦截、发布冻结、来源撤回生成替换任务、以及中断恢复后的期限处理。
