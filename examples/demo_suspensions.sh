#!/usr/bin/env bash
# 授权限时暂停调用示例。启动服务后执行：bash examples/demo_suspensions.sh
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
pretty() { if command -v jq >/dev/null 2>&1; then jq .; else cat; fi; }

FILM="film-suspension-demo"

echo "== 0. 健康检查 =="
curl -s "$BASE/health" | pretty

echo
echo "== 1. 录入两份授权（同影片同场馆，到期时间不同） =="
curl -s -X POST "$BASE/licenses" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"venue_id\": \"hall-1\",
  \"valid_from\": \"2026-10-01T00:00:00Z\", \"valid_to\": \"2026-10-10T23:59:59Z\",
  \"max_sessions\": 5, \"note\": \"早到期授权，默认优先消耗\"
}" | pretty
curl -s -X POST "$BASE/licenses" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"venue_id\": \"hall-1\",
  \"valid_from\": \"2026-10-01T00:00:00Z\", \"valid_to\": \"2026-10-31T23:59:59Z\",
  \"max_sessions\": 5, \"note\": \"兜底授权\"
}" | pretty

echo
echo "== 2. 暂停登记前核验一场：命中授权 1（该记录此后不受暂停影响） =="
curl -s -X POST "$BASE/verifications" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"venue_id\": \"hall-1\",
  \"start_time\": \"2026-10-05T19:30:00Z\", \"idempotency_key\": \"susp-demo-1\"
}" | pretty

echo
echo "== 3. 为授权 1 登记限时暂停（带时区时刻，左闭右开区间） =="
curl -s -X POST "$BASE/licenses/1/suspensions" -H 'Content-Type: application/json' -d '{
  "start_at": "2026-10-05T08:00:00+08:00",
  "end_at":   "2026-10-07T08:00:00+08:00",
  "reason": "版权方临时下架整改"
}' | pretty

echo
echo "== 4. 无效登记被拒绝且不留记录 =="
echo "-- 4a. 结束不晚于开始：422 --"
curl -s -w 'HTTP %{http_code}\n' -X POST "$BASE/licenses/1/suspensions" \
  -H 'Content-Type: application/json' -d '{
  "start_at": "2026-10-06T00:00:00Z", "end_at": "2026-10-06T00:00:00Z",
  "reason": "空区间"
}'
echo "-- 4b. 未知授权：404 --"
curl -s -w 'HTTP %{http_code}\n' -X POST "$BASE/licenses/999/suspensions" \
  -H 'Content-Type: application/json' -d '{
  "start_at": "2026-10-05T00:00:00Z", "end_at": "2026-10-06T00:00:00Z",
  "reason": "授权不存在"
}'
echo "-- 4c. 空原因：422 --"
curl -s -o /dev/null -w 'HTTP %{http_code}\n' -X POST "$BASE/licenses/1/suspensions" \
  -H 'Content-Type: application/json' -d '{
  "start_at": "2026-10-05T00:00:00Z", "end_at": "2026-10-06T00:00:00Z",
  "reason": ""
}'

echo
echo "== 5. 暂停期间核验：授权 1 被跳过，落到未暂停的授权 2 =="
curl -s -X POST "$BASE/verifications" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"venue_id\": \"hall-1\",
  \"start_time\": \"2026-10-06T19:30:00Z\", \"idempotency_key\": \"susp-demo-2\"
}" | pretty

echo
echo "== 6. 再暂停授权 2：覆盖时段的未撤销授权全部暂停 -> LICENSE_SUSPENDED =="
curl -s -X POST "$BASE/licenses/2/suspensions" -H 'Content-Type: application/json' -d '{
  "start_at": "2026-10-06T00:00:00Z", "end_at": "2026-10-08T00:00:00Z",
  "reason": "场馆消防检查，全部授权暂停"
}' | pretty
curl -s -X POST "$BASE/verifications" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"venue_id\": \"hall-1\",
  \"start_time\": \"2026-10-06T20:30:00Z\", \"idempotency_key\": \"susp-demo-3\"
}" | pretty

echo
echo "== 7. 区间右开边界：结束时刻起的场次不再受授权 1 的暂停影响 =="
echo "-- 7a. 10-07 08:00:00+08:00 = 授权1暂停结束时刻（右开不含）：授权1恢复，授权2仍暂停 -> 命中授权1 --"
curl -s -X POST "$BASE/verifications" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"venue_id\": \"hall-1\",
  \"start_time\": \"2026-10-07T08:00:00+08:00\", \"idempotency_key\": \"susp-demo-4\"
}" | pretty
echo "-- 7b. 10-08 起两份授权暂停均已结束，恢复核准（授权 1 优先） --"
curl -s -X POST "$BASE/verifications" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"venue_id\": \"hall-1\",
  \"start_time\": \"2026-10-08T19:30:00Z\", \"idempotency_key\": \"susp-demo-5\"
}" | pretty

echo
echo "== 8. 幂等重放：暂停登记前的 susp-demo-1 仍返回首次核准结果 =="
curl -s -X POST "$BASE/verifications" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"venue_id\": \"hall-1\",
  \"start_time\": \"2026-10-05T19:30:00Z\", \"idempotency_key\": \"susp-demo-1\"
}" | pretty

echo
echo "== 9. 查询暂停记录（登记后不可改） =="
echo "-- 9a. 授权 1 的暂停列表 --"
curl -s "$BASE/licenses/1/suspensions" | pretty
echo "-- 9b. 按暂停编号查询单条 --"
curl -s "$BASE/suspensions/1" | pretty

echo
echo "== 10. 授权额度视图：暂停期间未消耗授权 1，既有占用保持不变 =="
curl -s "$BASE/licenses?film_id=$FILM" | pretty
