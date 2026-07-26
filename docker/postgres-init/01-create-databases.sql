-- 初始化脚本：只在数据卷首次创建时执行一次。
-- 若改动本文件后未生效，需要 `docker compose down -v` 重建卷（会丢数据）。

-- Langfuse 自托管用独立 database，与业务库彻底隔离。
-- 这样 Alembic 天然管不到它的表，不必额外配置排除规则。
SELECT 'CREATE DATABASE langfuse'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'langfuse')\gexec

-- 测试库：集成测试默认用 testcontainers 起临时容器，
-- 但设置 TEST_DATABASE_URL 指向这里可以走快循环（代价是需自行清库）。
SELECT 'CREATE DATABASE expenseguard_test'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'expenseguard_test')\gexec
