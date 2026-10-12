# tdagent: Teradata 튜닝 에이전트 (로컬 오픈웨이트 LLM)

DBQL 에서 고부하 쿼리를 꺼내 **EXPLAIN 을 해석하고, Teradata 튜닝 포인트와 매칭해, 목표를 달성할 때까지
쿼리를 재작성·재측정**하는 에이전트입니다. 재작성으로 안 되는 부분은 **통계 수집·파티션·PI 권고**로 정리합니다.
31B 미만 로컬 오픈웨이트 모델로 동작하도록 설계했고, 자연어 → SQL 작성 기능도 보조로 들어 있습니다.

## 1. 동작 방식

```
DBQL 고부하 쿼리 / 붙여넣은 SQL
   │
   ▼  측정 (코드)  ── EXPLAIN 파싱: 단계별 접근경로·파티션·조인 방식·재분배/복제·신뢰도·예상 행/시간·HELPSTATS
   │               ── SQL 패턴 분석 (sqlglot) · DBQL 지표 (CPU 스큐, PJI, UII) · 테이블 스큐 (DBC.TableSizeV)
   ▼  튜닝 포인트 탐지 (코드) ── 각 탐지 결과를 스킬의 튜닝 포인트 ID(TP-xxx)에 매칭
   │
   ▼  목표 루프 ────────────────────────────────────────────────┐
   │   LLM 재작성 (탐지 결과 + 매칭된 TP 지식 + 테이블 정의 + 이전 시도 결과를 코드가 준비)   │
   │   가드: Teradata 방언 · 출력 컬럼 동일 · 조건/테이블 누락 의심 · (선택) 결과 비교        │
   │   EXPLAIN 재측정 → 개선됐을 때만 채택 (판정은 코드)                                    │
   └── 목표 달성 / 연속 개선 없음 / 시도 한도 까지 반복 ────────────────────────────────┘
   ▼
리포트: 튜닝 포인트별 진단(TP·근거·조치·등급) · 채택된 SQL · 통계 수집 스크립트 · 파티션/PI 제안 · 적용 순서
```

**문제가 있는지, 개선됐는지, 목표를 달성했는지는 모두 코드가 판정합니다.** LLM 은 재작성과 설명만 맡습니다.
작은 모델이 "다 됐다"고 해도 EXPLAIN 지표가 나아지지 않으면 채택하지 않습니다.

## 2. 튜닝 지식 = 스킬 (`skills/*.md`, 20개 · 튜닝 포인트 85개)

| 분류 | 스킬 | 내용 | TP 수 |
|---|---|---|---|
| 진단 | `tuning-workflow` | 튜닝 절차 - 병목 → 튜닝 포인트 매칭 → 조치 등급(A 재작성 / B 통계 / C 설계) → 검증 | - |
| 진단 | `explain-reading` | EXPLAIN 문구 해석 사전 - AMP 범위, 접근 경로, 스풀 배치, 조인 방식, 신뢰도, 병목 단계 | 1 |
| 진단 | `dbql` | DBQL 고부하 쿼리 선정과 지표 해석 - CPU·IO·스큐·Impact CPU·PJI·UII·스풀 | 5 |
| 조인 | `join-product-join` | Product join 원인별 - 조건 누락, OR 조인, 비동등/기간 조인, 함수 조인, 통계 오판 | 5 |
| 조인 | `join-geography` | 재분배·복제 줄이기 - PI 정렬, 조인 전 필터·집계, 조인 컬럼 타입 일치 | 4 |
| 조인 | `outer-join` | WHERE 의 안쪽 테이블 조건, ON 절 오해, 불필요한 OUTER JOIN | 4 |
| 접근경로 | `predicates` | sargability - 컬럼 함수, 암시적 형변환, 선행 % LIKE, OR 조건, 대형 IN 리스트 | 5 |
| 접근경로 | `partitioning` | 파티션 제거 확보, 파티션 미사용 전체 스캔, 파티션 추가 설계·마이그레이션 | 5 |
| 접근경로 | `querygrid` | QueryGrid 원격 조회 - 원격 계획 읽기, 컬럼·조건 pushdown, 원격 전체 스캔, 원격 추정 오류와 조인, 넓은 원격 컬럼 | 6 |
| 접근경로 | `indexes` | USI/NUSI, Join Index, Aggregate Join Index 사용 조건과 유지 비용 | 4 |
| 통계 | `statistics` | 수집 대상 우선순위, 문법, HELPSTATS 선별, SUMMARY 통계, 신뢰도, 오래된 통계, 다중 컬럼 통계 | 6 |
| 분산 | `skew` | 테이블 PI 스큐, 조인/집계 키 쏠림, NULL·기본값, 스풀 부족(2646), PRPD | 5 |
| 설계 | `primary-index-design` | PI 선택 기준, UPI/NUPI/NoPI, SET 테이블 중복 체크 비용, PI 변경 마이그레이션 | 4 |
| 설계 | `data-types` | 형변환, LATIN vs UNICODE, CHAR vs VARCHAR, MVC 압축, 정밀도 | 5 |
| 재작성 | `sql-rewrite` | 결과 보존 원칙, 불필요 컬럼 제거, 조기 필터, 반복 스캔 통합, UNION ALL, 단계화 | 5 |
| 재작성 | `subqueries` | NOT IN 의 NULL 함정, 상관 서브쿼리, 스칼라 서브쿼리, IN/EXISTS | 4 |
| 재작성 | `aggregation-olap` | DISTINCT vs GROUP BY, 조인 전 집계, PARTITION BY 없는 윈도우, QUALIFY, 프레임 | 6 |
| ETL | `etl-dml` | 빈 테이블 INSERT SELECT, 대량 UPDATE 대체, MERGE, DELETE ALL, volatile table 규칙 | 6 |
| 리소스 | `spool-locking` | 대용량 스풀, 스풀 부족, LOCKING FOR ACCESS, TOP/SAMPLE | 4 |
| 방언 | `teradata-sql-dialect` | TOP/QUALIFY, 날짜·문자열·캐스트, volatile table - SQL 을 쓸 때 항상 적용 | - |

각 튜닝 포인트는 같은 형식입니다:

```markdown
## TP-JOIN-02 OR 조인
- 신호: `ON a.k1 = b.k1 OR a.k2 = b.k2` → product join      ← 무엇을 보면 해당하는지
- 조치(A): 조건별 동등 조인으로 분리 후 UNION / UNION ALL     ← 조치와 등급
- 주의: ...                                                  ← 결과가 달라질 수 있는 지점
(재작성 예시 SQL)
```

**작은 모델은 어떤 문서를 읽어야 할지 잘 고르지 못하므로**, 튜닝 루프에서는 코드가 탐지 결과에 매칭된 TP 본문만
골라 프롬프트에 넣습니다 (`tuning.skill_chars` 예산 내). SQL 작성 모드에서는 `load_skill` 도구로 필요할 때 읽습니다.

**스킬은 팀이 직접 고치고 늘릴 수 있습니다.** `skills/` 에 같은 형식의 `.md` 를 추가하고 머리말의 `findings:` 에 탐지 코드를
적으면 그 문제가 발견될 때 자동으로 적용됩니다. 사내 표준(예: "통계는 매주 일요일 배치")도 여기에 적으면 됩니다.
`tests/test_tuning.py` 가 모든 탐지 코드 ↔ TP ID 연결이 끊기지 않았는지 검사합니다.

## 3. 탐지하는 튜닝 포인트

| 출처 | 탐지 (→ 튜닝 포인트) |
|---|---|
| EXPLAIN | product join (TP-JOIN-01/05, 입력 스풀 행 수로 크기 판단 - 몇 행짜리는 info), 소수 AMP 재분배 (TP-SKEW-06), 넓은 행 스풀 (bytes 기준), 대용량 복제 (TP-JOIN-12), 대용량 재분배 (TP-JOIN-11), 조인 형변환 (TP-JOIN-13), 파티션 테이블 전체 스캔 (TP-PART-01/02), 파티션 없는 대용량 전체 스캔 (TP-PART-05), no/low confidence (TP-STAT-01/02), HELPSTATS 권고 (TP-STAT-03), 대용량 스풀 (TP-RES-01), 병목 단계 (TP-EXP-01) |
| SQL 패턴 | 콤마 조인·조건 누락, OR 조인, 비동등 조인, 함수 조인, 컬럼 함수 조건(파티션 컬럼이면 critical), 문자 컬럼 vs 숫자 상수, 선행 % LIKE, 다른 컬럼 OR, 대형 IN, OUTER JOIN 의 WHERE 조건, 안 쓰는 OUTER JOIN, NOT IN 서브쿼리, 상관/스칼라 서브쿼리, **중간 결과의 안 쓰는 컬럼**, SELECT *, 반복 스캔, UNION, DISTINCT, PARTITION BY 없는 윈도우, UPDATE-조인, 전체 DELETE |
| QueryGrid | 원격 조회(원격 계획·원격 추정 행 수 대비), 원격 테이블 조건 없는 전체 전송 (TP-QG-01~03), **원격 추정에 기댄 조인/집계 폭증** (TP-QG-04), 원격 결과 행 폭 과다·`VARCHAR(MAX)` (TP-QG-06) |
| DBQL | CPU/IO 스큐, PJI ≥ 3 (product join 의심), UII ≥ 3 (불필요 IO 의심) |
| 테이블 | AMP 간 용량 스큐 (DBC.TableSizeV) → PI 검토 |

임계값은 `config.toml` 의 `[tuning]` 에서 조정합니다.

## 4. 결과 보존 장치

튜닝 재작성에서 가장 위험한 것은 **빨라졌지만 결과가 달라지는 것**입니다.

1. 출력 컬럼(이름·순서)이 원본과 다르면 무효
2. Teradata 방언 오류(LIMIT 등)면 무효
3. **조건(WHERE/ON/HAVING)에서 컬럼이 사라졌거나, 참조 테이블·OUTER JOIN 구성이 바뀌면** 결과 보존 의심 → 기각하고
   이유를 다음 시도에 알려줌 (예: OR 조인의 한쪽 조건을 빠뜨린 재작성)
4. (선택) **결과 동일성 검증**: 실행이 허용되면 `원본 EXCEPT ALL 재작성` 과 반대 방향 건수를 실제로 세어 0 이 아니면 기각.
   두 쿼리를 실제 실행하므로 무거운 쿼리는 비용을 감안하세요. WITH / ORDER BY / TOP 이 있으면 건너뜁니다.

그래도 최종 반영 전에는 사람이 diff 와 "가정" 항목을 확인해야 합니다.

## 5. 모델 선택: Qwen3.8-27B (`qwen3.8:27b`)

| 후보 | 크기 | 판단 |
|---|---|---|
| **Qwen3.8-27B** ✅ | 27B dense, Q4 약 17GB | 2026년 8월 출시, Apache 2.0, 26만 토큰 컨텍스트. 코딩 성능이 직전 세대보다 크게 향상 (Qwen 공개 기준 SWE-bench Pro 61.7, Qwen3.6-27B는 53.5) |
| Qwen3-Coder-30B-A3B | 30B MoE (활성 3.3B), Q4 약 19GB | 2025년 모델이라 정확도는 낮지만 **훨씬 빠름**. Qwen3.8 응답이 너무 느리면 1순위 대안 |
| gpt-oss-20b | 21B MoE, 약 14GB | 도구 호출 양호, 가벼움. GPU 메모리가 16GB 이하일 때 |

선정 이유는 다음과 같습니다.
- SQL 에이전트는 빠른 것보다 **틀리지 않는 것**이 중요합니다. 31B 미만에서 가장 최신이고 코딩 성능이 가장 좋은 모델입니다.
- GPU 한 장(24GB면 컨텍스트까지 여유)에 올라갑니다.

**속도 주의:** dense 모델이라 토큰마다 27B 전체를 계산합니다. 활성 파라미터가 3.3B인 MoE 모델보다 생성 속도가 몇 배 느립니다. 에이전트는 요청 하나에 모델을 4~8번 부르므로, 응답 시간이 길게 느껴질 수 있습니다.

**thinking 모드:** Qwen3.x는 답하기 전에 추론(thinking)을 할 수 있습니다. 정확도에는 도움이 되지만 더 느려집니다. `max_tokens`를 넉넉하게(기본 8192) 잡아두었고, `<think>` 내용은 답변에서 자동으로 제거합니다. 속도가 더 중요하면 서버 설정이나 `[llm.extra_body]`로 thinking을 끌 수 있습니다 (`config.example.toml` 참고).

모델은 `config.toml`의 `model` 한 줄로 바꿀 수 있습니다. **팀의 실제 쿼리 10~20개로 Qwen3.8-27B와 qwen3-coder:30b를 직접 비교**해 보시길 권합니다.

### 작은 모델의 약점을 코드로 보완한 방법 (SQL 작성 모드)

| 작은 모델의 약점 | 이 에이전트의 대응 |
|---|---|
| 없는 테이블/컬럼을 지어냄 | 스키마 카탈로그로 **테이블·컬럼 실존 여부를 코드로 검사** (sqlglot). DB가 연결돼 있으면 실제 `EXPLAIN`으로 검사 |
| LIMIT, ILIKE, `::` 같은 타 DB 문법 | 방언 규칙을 **정적 검사**하고, 오류가 있으면 자동으로 재작성을 요청 (최대 2회) |
| "다 됐다"는 말을 믿을 수 없음 | 최종 SQL은 모델 판단과 관계없이 **항상 코드로 검증** |
| 스키마 전체를 컨텍스트에 못 담음 | `search_schema` → `describe_table`로 필요한 테이블만 조회 |
| 같은 행동 반복 / 루프 | 동일 호출 3회째 차단, 최대 단계 수 초과 시 답변 강제 |
| 긴 대화에서 품질 저하 | 이전 턴은 "요청 + 최종 답"만 남기고 중간 도구 기록은 버림 |
| 도구 호출 JSON 깨짐 | 오류 메시지를 돌려줘서 재시도. `<tool_call>` 텍스트 형태로 나온 호출도 복구 |
| 업무 용어를 모름 | `knowledge/*.md`(용어집, 예시 쿼리)를 시스템 프롬프트에 주입 |

정적 검사가 잡는 것:
- **오류**: LIMIT, OFFSET, ILIKE, 백틱, `::`, IFNULL, NOW(), DATEDIFF, 카탈로그에 없는 테이블·컬럼
- **경고**: product join, 콤마 조인, WHERE 절에서 컬럼을 함수로 감싼 경우(파티션 제거 불가), `NOT IN (서브쿼리)`, 윈도우 프레임 미지정, 서브쿼리 ORDER BY, `SELECT *`

`EXPLAIN` 해석 힌트: product join, 재분배, 전체 복제, all-rows scan, 통계 부재, 파티션 제거 여부, 예상 시간

## 6. 설치

```bash
# 1) 모델 서버 (Ollama 예시)
ollama pull qwen3.8:27b    # 정확한 태그 이름은 Ollama 라이브러리에서 확인
# 중요: Ollama 기본 컨텍스트는 작아서 시스템 프롬프트와 스키마가 잘립니다. 32K 이상으로 실행하세요.
OLLAMA_CONTEXT_LENGTH=32768 ollama serve

# 2) 에이전트
pip install -e ".[web,teradata]"  # Teradata 접속이 필요 없으면: pip install -e ".[web]"
cp config.example.toml config.toml
tdagent doctor                    # LLM / Teradata 연결 확인
tdagent web                       # 브라우저에서 http://127.0.0.1:8501 이 열림
```

vLLM, llama.cpp server, LM Studio 등 **OpenAI 호환 서버**라면 `base_url`만 바꿔서 쓸 수 있습니다.

## 7. 사용법

### 웹 UI (주로 쓰는 방법)

```bash
tdagent web                 # http://127.0.0.1:8501 (이 PC 에서만 접속 가능)
tdagent web --port 8600 --no-browser
```

사이드바에서 모드를 고릅니다.

**🔧 튜닝 (기본 화면)**

| 영역 | 기능 |
|---|---|
| 튜닝 대상 | **SQL 붙여넣기** / **DBQL 에서 찾기**(기간·사용자·최소 CPU·정렬[CPU, IO, Impact CPU, 스큐, 스풀]로 조회 → 불러오기) / **EXPLAIN 붙여넣기**(DB 미연결 시) |
| 🎯 튜닝 목표 | product join 제거, 파티션 전체 스캔 제거, 대용량 복제 제거, critical 0건, 예상 시간 N% 단축, 최대 시도 횟수, 추가 목표(자유 입력), 결과 동일성 검증 |
| 버튼 | **🔍 진단만** (튜닝 포인트·권고만) / **🚀 목표 달성까지 튜닝** (반복 재작성) |
| 결과 지표 | 예상 시간, product join, 파티션 미적용 스캔, 통계 없음 단계, critical 수 (원본 대비 증감) + 목표 기준별 ✅/❌ |
| 탭 | **튜닝 포인트**(근거 + 매칭된 TP 지식 펼쳐보기 + 해결 여부) · **반복 과정**(시도별 채택/기각 사유) · **최종 SQL**(diff, 저장) · **권고 스크립트**(통계 수집 SQL, 파티션/PI 후보) · **리포트**(.md 저장) · **EXPLAIN**(전/후) |

**📚 튜닝 지식**: 에이전트가 적용하는 스킬(`skills/*.md`)과 튜닝 포인트를 검색·열람합니다.

**✍️ SQL 작성** (보조 기능)

| 영역 | 기능 |
|---|---|
| 왼쪽 대화 | 자연어 요청. **SQL 을 그대로 붙여넣어도 됨** (Shift+Enter 줄바꿈). 에이전트 작업 과정(도구 호출, 자동 재작성)을 펼쳐 볼 수 있음 |
| 오른쪽 "현재 SQL" | 에이전트가 만든 최신 쿼리. 문법 강조 + **복사 버튼**. "직접 편집"으로 손보거나 붙여넣기 |
| 현재 SQL 첨부 | 켜져 있으면 다음 요청에 현재 SQL 이 같이 전달됨 → "할인율 추가해줘", "왜 느려?" 처럼 이어서 수정·분석 |
| 버튼 | 검증 · EXPLAIN (Teradata 연결 시) · 실행 (allow_execute 시) · .sql 저장 |
| 탭 | 검증 결과 · **변경 비교**(이전 버전과 diff) · EXPLAIN 힌트 · 실행 결과 표 |
| 사이드바 | 새 쿼리(대화 초기화), schema/knowledge 수정 후 다시 읽기, **Teradata 접속 설정** |

#### Teradata 접속 설정 (사이드바 🔌)

1. Host, User, Password, 인증 방식(TD2/LDAP 등), 데이터베이스를 입력하고 **연결 테스트** (저장하지 않고 접속만 확인)
2. **저장**: `config.toml` 의 `[teradata]` 에 저장됩니다. 주석과 다른 설정은 그대로 유지됩니다.
   - **"비밀번호 저장"** 이 체크되어 있으면 비밀번호는 **OS 자격 증명 저장소**(Windows 자격 증명 관리자 / macOS 키체인)에 저장되고, 설정 파일에는 절대 쓰지 않습니다.
   - 다음부터는 비밀번호 칸이 비어 있고 "저장됨" 으로 표시됩니다. **비워 둔 채 저장하면 기존 비밀번호를 그대로 사용**하고, 바꿀 때만 새로 입력하면 됩니다.
   - 체크를 해제하고 저장하면 저장된 비밀번호를 삭제합니다 (이번 실행 동안만 기억).
3. **스키마 가져오기**: 저장된 접속 정보로 DBC 에서 테이블·컬럼·코멘트·PI 를 가져와 `schema/catalog.json` 을 만듭니다.

비밀번호를 읽는 순서는 환경변수 `TD_PASSWORD` → `config.toml` → OS 자격 증명 저장소입니다. CLI 도 저장된 비밀번호를 같이 씁니다.
OS 자격 증명 저장소가 없는 환경(일부 Linux 서버)에서는 저장되지 않고 이번 실행 동안만 기억합니다.

보안을 위해 웹 서버는 `127.0.0.1` 에만 열립니다 (DB 접속 정보를 가진 앱이므로 다른 PC 에서 접속 불가).

### CLI

```bash
tdagent dbql --days 1 --by skew --top 20       # DBQL 고부하 쿼리 (cpu|io|impact|skew|spool)
tdagent dbql --days 1 --save top               # 1위 쿼리 전체 SQL 을 query_<QueryID>.sql 로 저장
tdagent tune query_123.sql --time 30 --out out # 목표(예상 시간 30% 단축 + product join/파티션 스캔 제거)까지 반복
tdagent tune q.sql --analyze-only              # 진단만
tdagent tune q.sql --explain explain.txt       # DB 미연결: 붙여넣은 EXPLAIN 으로 진단 + 검증 안 된 재작성 제안 1개
tdagent tune q.sql --result-check              # 채택 전 원본/재작성 결과를 EXCEPT ALL 로 비교 (실행 허용 필요)
tdagent skills 파티션                           # 튜닝 포인트 카탈로그 검색
tdagent explain-check top50.txt --plans        # 실제 EXPLAIN 추출본으로 파서·탐지 점검 (놓친 문구가 있으면 exit 1)
tdagent chat                                   # 대화형 (수정 요청을 이어서 할 수 있음)
tdagent ask "지난달 채널별 순매출"
tdagent ask "고객명 컬럼 추가하고 VIP만" -f q.sql  # 기존 쿼리 수정
tdagent analyze slow.sql --note "30분 걸림"      # 분석 + 개선안
tdagent check q.sql                            # LLM 없이 검사만 (CI 등에 사용, 실패 시 exit 1)
tdagent sync-schema --db SALES_DB CUST_DB      # DBC 에서 스키마 카탈로그 생성
tdagent -v chat                                # 도구 결과까지 보기
```

대화형 모드 명령어: `"""`(여러 줄 입력 시작/끝), `/file q.sql 요청`, `/sql`, `/reset`, `/exit`

## 8. 정확도를 높이는 법 (중요한 순서대로)

1. **Teradata 연결**(`teradata.enabled = true`): 튜닝 루프는 EXPLAIN 으로 매 시도를 측정해야 제대로 돕니다.
   `tuning.helpstats = true`(기본)면 옵티마이저의 통계 권고도 함께 받습니다.
2. **스키마 카탈로그**: `sync-schema`를 실행하거나 DDL을 `schema/*.sql`에 넣으세요. 파티션(PPI) 정의가 있어야
   "파티션 테이블 전체 스캔" 을 정확히 잡고, 컬럼 타입이 있어야 암시적 형변환을 잡습니다. **컬럼 코멘트(한글 설명)**는 검색 품질을 좌우합니다.
3. **스킬 보강**: 사내 튜닝 사례·표준을 `skills/*.md` 에 튜닝 포인트 형식으로 추가하세요. 실제로 효과를 본 Before/After 예시가 가장 효과적입니다.
4. **예시 쿼리**: `knowledge/examples.md`에 검증된 "요청 → 쿼리" 쌍을 10~30개 넣으세요. 작은 모델에게는 설명보다 예시가 훨씬 효과적입니다.
5. **용어집**: `knowledge/glossary.md`에 "매출", "활성 고객"처럼 팀에서 쓰는 정의를 적으세요 (SQL 작성 모드).

`schema/sample_sales.sql`과 `knowledge/*.md`는 예시이므로 실제 환경에 맞게 교체하세요.

## 9. 안전장치

- 기본 설정에서는 **쿼리를 실행하지 않습니다** (EXPLAIN과 메타데이터 조회만).
- `allow_execute = true`여도 단일 SELECT/WITH/HELP/SHOW만 허용하고, 쓰기·DDL 키워드가 있으면 거부하며, 결과는 `max_rows`까지만 가져옵니다.
- 비밀번호는 웹 UI 에서 저장하면 OS 자격 증명 저장소에, 아니면 환경변수 `TD_PASSWORD` 로 넣으세요. 설정 파일에는 쓰지 않습니다 (`config.toml` 은 `.gitignore` 에도 포함).

- 통계 수집·파티션·PI 변경은 **권고 스크립트로만** 제공합니다. 에이전트가 DDL 을 실행하지 않습니다.
- EXPLAIN 은 실행 계획만 만들고 쿼리를 실행하지 않습니다. INSERT-SELECT 같은 DML 도 EXPLAIN 으로만 분석합니다.

### EXPLAIN 파서 검증

Teradata 20.0 실제 EXPLAIN 77건(성공 65, 권한/객체 오류 12)으로 보정했습니다. 그중 31건은
`DIAGNOSTIC HELPSTATS ON FOR SESSION` 상태로 추출한 INSERT-SELECT, DELETE ALL, 매크로 EXEC, QueryGrid(Oracle·SQL Server)
원격 카탈로그 적재 ETL 입니다. 뷰 별칭(`DB.A in view V`),
스풀 번호 없는 크기 문구, 키 없는/상수 키 재분배(`to few AMPs`), OUTER JOIN 방식, `all partitions`/초대형 파티션 수,
총 예상 시간이 없는 계획(단계 합으로 대체), IPE 머리말, 조건·재분배 키 안의 `TRIM(BOTH FROM ...)` 를 처리합니다.
스풀 번호를 따라가며 단계별 입력 행 수와 원천 테이블(lineage)을 계산해 product join 크기와 통계 권고 대상에 씁니다.
HELPSTATS 는 여러 줄로 이어진 권고, `COLLECT SUMMARY STATISTICS`, 신뢰도(High/Medium), FINAL PLAN / OTHER PLANS 구분을 읽고,
**우선 적용**(High + 최종 계획 + 4개 이하 컬럼, SUMMARY)과 **선택 검토**(Medium, 다른 계획용, 5개 이상 컬럼 조합,
`DBC.` 딕셔너리)로 나눕니다. DML 단계(`MERGE step into T`, `INSERT step into T`, `full table deletion`)와
QueryGrid 원격 계획 블록(`BEGIN EXPLAIN FOR REMOTE QUERY`)도 구조화합니다 (Oracle 계획 행의 테이블/인덱스 구분, 전체 스캔,
원격 추정 행 수, 계획을 주지 않는 커넥터, `IS NOT NULL` 뿐인 조건은 "선택 조건 없음"). 원격 데이터가 어느 단계로 흘러가는지
추적해서, 원격 추정(no confidence 60,000행)으로 부풀려진 조인을 하나의 문제(REMOTE_ESTIMATE_JOIN)로 보고하고 거기서 파생된
대용량 스풀·재분배·통계 경고는 따로 내지 않습니다.

새 환경의 EXPLAIN 을 추출하면 `tdagent explain-check` 로 놓치는 문구가 있는지 바로 확인할 수 있고,
`TDAGENT_EXPLAIN_CORPUS=<파일1>:<파일2> pytest` 로 회귀 테스트에 넣을 수 있습니다 (사내 정보가 담긴 원본은 저장소에 넣지 않음).

애플리케이션 SQL 의 `?` 파라미터 마커는 더미 값 `'1'` 로 바인딩해 EXPLAIN 하고, 리포트에 그 사실을 표시합니다.

### 한계
- 예상 시간은 옵티마이저 추정치입니다. 효과는 반영 후 DBQL 의 CPU/IO 로 확인하세요.
- DBQL SQL 이 volatile table 을 참조하면 지금 세션에 그 테이블이 없어 EXPLAIN 이 실패합니다 (앞 단계 SQL 필요).
- DB 미연결(EXPLAIN 붙여넣기) 모드에서는 재작성 결과를 검증할 수 없어 "검증되지 않은 제안" 1개만 만듭니다.
- 긴 ETL 쿼리는 프롬프트가 커집니다. 모델 서버 컨텍스트를 32K~64K 로 잡으세요.

## 구조

```
skills/            튜닝 지식 (스킬 19개, 튜닝 포인트 77개) - 팀이 직접 수정·추가
tdagent/
  tuning/
    explain.py     EXPLAIN 파서 (단계·접근경로·파티션·조인·배치·신뢰도·행/시간·HELPSTATS)
    findings.py    탐지 코드 ↔ 튜닝 포인트 매핑, EXPLAIN/DBQL/테이블 기반 탐지
    sqlcheck.py    SQL 패턴 탐지, 미사용 컬럼, 결과 보존 가드
    goal.py        목표 정의와 판정, 개선 여부 판정
    recommend.py   통계 수집 / 파티션 / PI 권고 후보 (결정적)
    session.py     목표 달성 루프 + 리포트
    dbql.py        DBQL 고부하 쿼리 조회, SQL 전문 복원
    prompts.py     재작성 / 리포트 프롬프트
  skills.py        스킬 로더 (머리말, 튜닝 포인트 파싱, 탐지 → 스킬 매칭)
  webapp.py        로컬 웹 UI 진입점 (모드 전환, SQL 작성, 접속 설정)
  webui_tuning.py  튜닝 / 튜닝 지식 화면
  agent.py · tools.py · prompts.py   SQL 작성 에이전트 (도구 호출, load_skill)
  sql_lint.py · schema.py · teradata.py · credentials.py · config.py · cli.py
```

테스트: `pip install -e ".[dev,web]" && pytest`
