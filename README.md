# tdagent: Teradata SQL Agent (로컬 오픈웨이트 LLM)

자연어 요청으로 **Teradata SQL을 작성·수정·분석**하는 에이전트입니다.
로컬에서 돌리는 31B 미만 오픈웨이트 모델로 동작하도록 설계했습니다.

```
td> 최근 3개월 고객 등급별 매출 상위 10개
→ search_schema(query='고객 등급 매출')
→ describe_table(table='SALES_DB.CUSTOMER')
↻ 자동 검증 실패 → 재작성 요청 (1회차)        ← 모델이 LIMIT 을 썼다가 자동으로 교정됨
SELECT TOP 10 c.GRADE_CD, SUM(t.SALE_AMT) AS amt ...
╭─ 자동 검증 ─ 결과: 통과 (오류 0, 경고 0) ─╮
```

## 1. 모델 선택: Qwen3.8-27B (`qwen3.8:27b`)

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

## 2. 작은 모델의 약점을 코드로 보완한 방법

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

## 3. 설치

```bash
# 1) 모델 서버 (Ollama 예시)
ollama pull qwen3.8:27b    # 정확한 태그 이름은 Ollama 라이브러리에서 확인
# 중요: Ollama 기본 컨텍스트는 작아서 시스템 프롬프트와 스키마가 잘립니다. 32K 이상으로 실행하세요.
OLLAMA_CONTEXT_LENGTH=32768 ollama serve

# 2) 에이전트
pip install -e ".[teradata]"      # Teradata 접속이 필요 없으면: pip install -e .
cp config.example.toml config.toml
tdagent doctor                    # LLM / Teradata 연결 확인
```

vLLM, llama.cpp server, LM Studio 등 **OpenAI 호환 서버**라면 `base_url`만 바꿔서 쓸 수 있습니다.

## 4. 사용법

```bash
tdagent chat                                   # 대화형 (수정 요청을 이어서 할 수 있음)
tdagent ask "지난달 채널별 순매출"
tdagent ask "고객명 컬럼 추가하고 VIP만" -f q.sql  # 기존 쿼리 수정
tdagent analyze slow.sql --note "30분 걸림"      # 분석 + 개선안
tdagent check q.sql                            # LLM 없이 검사만 (CI 등에 사용, 실패 시 exit 1)
tdagent sync-schema --db SALES_DB CUST_DB      # DBC 에서 스키마 카탈로그 생성
tdagent -v chat                                # 도구 결과까지 보기
```

대화형 모드 명령어: `"""`(여러 줄 입력 시작/끝), `/file q.sql 요청`, `/sql`, `/reset`, `/exit`

## 5. 정확도를 높이는 법 (중요한 순서대로)

1. **스키마 카탈로그**: `sync-schema`를 실행하거나 DDL을 `schema/*.sql`에 넣으세요. **컬럼 코멘트(한글 설명)**가 검색 품질을 좌우합니다.
2. **예시 쿼리**: `knowledge/examples.md`에 검증된 "요청 → 쿼리" 쌍을 10~30개 넣으세요. 작은 모델에게는 설명보다 예시가 훨씬 효과적입니다.
3. **용어집**: `knowledge/glossary.md`에 "매출", "활성 고객"처럼 팀에서 쓰는 정의를 적으세요.
4. **Teradata 연결**(`teradata.enabled = true`): 실제 `EXPLAIN`이 가장 확실한 검증기입니다. 쿼리를 실행하지 않고 문법·객체·권한만 확인합니다.

`schema/sample_sales.sql`과 `knowledge/*.md`는 예시이므로 실제 환경에 맞게 교체하세요.

## 6. 안전장치

- 기본 설정에서는 **쿼리를 실행하지 않습니다** (EXPLAIN과 메타데이터 조회만).
- `allow_execute = true`여도 단일 SELECT/WITH/HELP/SHOW만 허용하고, 쓰기·DDL 키워드가 있으면 거부하며, 결과는 `max_rows`까지만 가져옵니다.
- 비밀번호는 환경변수 `TD_PASSWORD`로 넣으세요. `config.toml`은 `.gitignore`에 포함되어 있습니다.

## 7. CLI만으로 충분할까, UI가 필요할까?

**지금은 CLI로 시작하는 것을 권장합니다.**
- 초기에는 프롬프트, 예시 쿼리, 모델을 계속 바꿔가며 **품질을 튜닝하는 기간**입니다. CLI가 가장 빠르게 반복할 수 있습니다.
- `tdagent check`는 종료 코드를 반환하므로 스크립트나 CI에 바로 붙일 수 있습니다.
- SQL을 다루는 사용자는 대부분 터미널과 편집기에 익숙합니다.

**다음 경우에는 웹 UI를 추가하세요.**
- 터미널을 쓰지 않는 현업 분석가에게 배포할 때
- GPU 서버 한 대에 모델을 올리고 **여러 명이 브라우저로 공유**할 때
- 결과 표 미리보기, 복사 버튼, 쿼리 이력 공유가 필요할 때

에이전트 핵심(`Agent.run()` + 이벤트 콜백)은 UI와 분리되어 있어서, Streamlit이나 Gradio 화면은 100줄 정도로 붙일 수 있습니다.

## 구조

```
tdagent/
  agent.py     에이전트 루프 (도구 실행, 자동 검증·재작성, 반복 차단, 히스토리 압축)
  llm.py       OpenAI 호환 클라이언트 (+ 텍스트 도구호출 복구, <think> 제거)
  tools.py     search_schema / describe_table / validate_sql / run_query
  sql_lint.py  Teradata 방언 정적 검사, 카탈로그 검사, EXPLAIN 해석, 읽기전용 가드
  schema.py    스키마 카탈로그 (DDL 파싱, JSON, 한글 키워드 검색)
  teradata.py  teradatasql 래퍼 (EXPLAIN, DBC 메타데이터, 읽기전용 실행)
  prompts.py   시스템 프롬프트 + Teradata 방언 치트시트
  cli.py       CLI
```

테스트: `pip install -e ".[dev]" && pytest`
