-- 在宝塔面板数据库登记站点（sites / domain 两张表）
-- 注意：`index` 是 SQLite 保留字，必须用反引号包裹
INSERT INTO sites (name, path, status, `index`, ps, addtime)
VALUES (
  'homework.aws.shiliu153.cn',
  '/www/wwwroot/homework.aws.shiliu153.cn',
  '1',
  'index.html',
  '学习通待办中心（反向代理 127.0.0.1:8000）',
  '2026-11-01 20:05:00'
);

INSERT INTO domain (pid, name, port, addtime)
VALUES (
  (SELECT id FROM sites WHERE name = 'homework.aws.shiliu153.cn'),
  'homework.aws.shiliu153.cn',
  80,
  '2026-11-01 20:05:00'
);
