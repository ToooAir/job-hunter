# 工作清單：Azure Luna 換裝後的下一批（2026-09-05）

依期望值排序。每項含：目的、實作計劃、注意事項、驗收判準。
**狀態：全部待執行，未開工。** 前置：09-05 07:00 第一次無人值守 Azure 日跑先看 log
（開頭應有 `LLM provider: azure | chat=gpt-5.6-luna … | translation=gpt-5-nano … | emb=text-embedding-3-small …`，
且無 `Scoring failed` / `Marked job`）。

## 現況數字（決定排序的依據）

| 指標 | 數字 | 意義 |
|---|---|---|
| 09-02 起投遞 | 9 筆 / 3 天 | plan §4 停損判準「6 週 ≥250 投遞」照此節奏只到 ~130，判準量不出結論 |
| 一面 / 二面（peak_stage） | 9 / 0 | 洞在面試層 |
| Luna 近 36h 評分 | A 283 / B 288 / C 3183 | A 門檻 76 後 A:B ≈ 1:1 |
| lever 來源、公司 jobgether、地點裸國名的 A/B 已評分列 | US 81、UAE 11、Saudi 9、Chile 2 | 漏過 geo 閘，白花評分且污染佇列供給 |

---

## 1. LLM 成本護欄（最先做）

**目的**：Mistral 額度歸零是安全失敗（停機），Azure 是刷卡（無上限）。現在 `CALL_STATS` 只是
呼叫次數計數器，token 與費用沒有落盤，09-04「一次重評燒光額度」在 Azure 上的變體是帳單。

**實作計劃**
1. `utils/llm.py`：在 `chat_completion` / `chat_parse` 回傳前讀 `resp.usage`
   （`prompt_tokens`、`prompt_tokens_details.cached_tokens`、`completion_tokens`），
   新增 `embed(client, model, inputs)` 包裝 `client.embeddings.create` 並同樣記 usage；
   `phase2_scorer._batch_embed`、`retrieve_context`、`kb_loader`、`check_api` 改走它。
2. 價格表：`_PRICES = {model_substring: (in, cached, out) $/M}`，預設填 luna 0.20/0.10/1.20、
   nano、text-embedding-3-small；可由 env `LLM_PRICE_<KIND>` 覆寫。未知模型記 token、費用記 None。
3. 落盤：每次呼叫 append 一行 JSON 到 `data/llm_usage.jsonl`
   （ts、script、model、kind、tokens、est_usd、job_id 若有）。單行 < 4 KB 以 `O_APPEND` 寫，
   三個容器（pipeline / dashboard / apply_api）同時寫也不會交錯。
4. 預算閘：env `LLM_DAILY_BUDGET_USD`（未設 = 不限）。**建議值 2.0**：穩態每日評分 60–130 筆
   （近 14 天中位 ~95，其中 ~55% 德文），Luna $0.0015/次 + nano 翻譯 + embedding + apply_api 零星
   ≈ $0.2/天、$6/月；$2 是穩態 10 倍，失控迴圈或誤觸全池重評在 ~1,300 筆處被截停，而一次刻意的
   700 筆 A/B 池重評（~$1）不會被誤殺。刻意大重評時用腳本 `--budget` 旗標覆寫，不改 `.env`
   （env 改了要 `docker compose up -d` 才生效）。另在 Azure Cost Management 設 $15/月 alert 當第二道。`phase2_scorer` worker 每次呼叫前
   把「今日 jsonl 累計 + 本程序累計」與預算比，超過 → raise `TransientAbort("daily LLM budget …")`，
   走既有 exit 75 → scheduler 退避，職缺留 un-scored，隔日繼續。
5. `phase3_dashboard` 加一張小卡：今日 / 本月估算費用、呼叫數、依模型拆分（讀 jsonl，不打 API）。

**注意**
- 估算費用只是估算，Azure 帳單才是權威；卡片要寫「est.」。
- 預算閘不要用 `mark_error`，一律 `TransientAbort`（09-04 教訓：429 路徑把 117 筆標永久錯誤）。
- 退避重試每次會再花一點錢才發現超額；可接受，但 log 要一眼看出是預算不是故障。
- Azure 快取 token 折扣需在價格表分開列，否則高估 ~30%（Luna 實測 ~45% cached input）。
- `.env` 由使用者加 `LLM_DAILY_BUDGET_USD`；`.env.example` 同步、值虛構。
- 測試：`tests/test_apply_llm` 的 `FakeClient` 需補 `usage` 物件，否則新包裝會 AttributeError。

**驗收**：跑一次 `phase2_scorer.py` 後 jsonl 有行、dashboard 卡片有數字；把預算設 0.01 手動跑，
應以 exit 75 結束、無任何 job 被標 error。

---

## 2. 翻譯步驟退役（先實驗，再決定）

**目的**：Luna 原生讀德文；翻譯僅存理由是 KB 檢索的英文向量，而 text-embedding-3-small 多語。
翻譯會把「Deutsch C1 / verhandlungssicher」翻掉或翻歪，是 hallucinated `de_required` 的一個源頭；
退役同時省每天 ~100 次 nano 呼叫、延遲與一個 429 面。

**實作計劃**
- **Step A 讀 only 實驗**（scratch 腳本，沿用 `ab_openai.py` 骨架，qdrant 複製到 `/tmp` 避 lock）：
  1. 抽 40 筆有 `translated_jd_text` 的德文 JD。
  2. 原文向量 vs 譯文向量各做 `_qdrant_query(top_k=5)`，算 top-5 重疊率與 top-1 分數分布
     （跨語言相似度通常較低，看 0.35 門檻是否仍有 context）。
  3. 同 40 筆用 Luna 以原文 / 譯文各評一次：等級一致率、`jd_language_req` 對照 JD 原文裁決
     （依 09-04 教訓：以 JD 原文為準，不以現任結果為準）。
  4. 判準：重疊 ≥ 4/5 中位、等級一致 ≥ 36/40、語言標籤原文版錯誤數 ≤ 譯文版 → 進 Step B。
- **Step B 程式改動**（僅在 A 通過後）：
  1. `phase2_scorer.py` 655–672 翻譯迴圈加 env 開關 `TRANSLATE_GERMAN`（預設 `1` 保持現狀；
     使用者切 `0`）。
  2. 關閉時 `effective_jd` 三處（711、887、1152）與 embedding 文本（683）一律用 `raw_jd_text`，
     舊列已存的 `translated_jd_text` 保留不刪、不再讀，避免同一池新舊不一致。
  3. `build_prompt` / grading rules 補一句「JD may be in German; output stays in English」，
     確認 CL 輸出語言規則明確。
  4. `.env.example`、README 補說明。
- 實驗費用約 $0.1。

**注意**
- `_detect_german` 是否還有其他消費者（語言標籤前置判定）要先 grep，別把它一起拔掉。
- 檢索門檻 0.35 是以英文查詢量的；德文查詢若 top-1 掉到 0.3 以下要另設或改 top_k 主導。
- 回滾就是 env 切回 `1`，零資料遷移。
- 不重評歷史池，只影響新進職缺。

**驗收**：Step A 報表四項判準；Step B 上線後一週 `de_required` 誤標抽查 20 筆 ≤ 1。

---

## 3. 停損判準改口徑 + 草稿老化提醒

**目的**：plan §4 的「投遞 ≥250」量的是人力，不是管線；照現在 3 筆/天在 6 週後只會得到
「資料不足」。草稿會腐爛（plan Phase 0 第 4 條）但目前沒有任何東西在催。

**實作計劃**
1. `docs/plan_2026-09_mid_level_offer_TC.md` §4 改寫，**數字由使用者拍板**。依據：近 8 週每週投遞
   25–45（均值 ~37，09-02 起 3 天 9 筆是短期低點）；歷史一面率 9/682 = 1.3%，約每月 1 場；
   一面多在投遞後 1–3 週出現（21 天成熟窗），所以投遞視窗與評估日要分開。建議：
   - **投遞視窗 6 週（09-02 → 10-14），評估日 8 週（10-28）**。
   - 投遞 ≥ 200 且一面 = 0 → 供給非槓桿，停管線投資（基準率下期望 ~2.6 場，P(0) ≈ 7%，
     是這個 n 能給的最強訊號；「≤ 2」的 P ≈ 50%，量不出東西，原 250/≤2 判準因此作廢）。
   - 投遞 ≥ 200 且一面 = 1 → 弱訊號：不擴量、不砍，再延 4 週。
   - 一面 ≥ 3 且二面 0 → 面試層問題（維持原句）。
   - 供給判準：待投草稿連續兩週在週中歸零 → 供給端瓶頸，回頭看來源（現在待投 21 張）。
   - 人力判準：投遞 < 20/週連續兩週 → 瓶頸是投遞時間，任何工程項目都不該以「加抽數」為由。
2. Apply Review（`pages/1_Apply_Review.py`）：先查草稿存放位置（`application_snapshots` 或
   `jobs.cover_letter_draft` + status），每張卡片顯示「生成 N 天」，> 7 天標紅、預設排序最舊在前。
3. `phase3_dashboard` 首頁加一行：待投 N 張、最舊 X 天、本週已投 Y。

**注意**
- 這項的工程量在 UI，決策量在 §4 數字；先改文件再改 UI。
- 「本週已投」用 `applied_at`，與 `47f20d0` 一致用 Europe/Berlin 時鐘。
- 不做自動撤回老草稿（可能把還能投的砍掉），只提醒。

**驗收**：§4 新句寫入並 commit；Apply Review 有天數與紅標。

---

## 4. 面試 brief 注入歷史一面題

**目的**：`interview_records` 已有 5 筆，retro 說要記非技術題原文；brief 目前只看 JD + KB，
沒用到「上次被問什麼、答得如何」，而 9→0 的洞正在這裡。

**實作計劃**
1. `phase2_scorer.generate_brief_for_job`：在 `context` 之後，用 `utils.db.get_interview_records`
   的變體（新增 `get_all_interview_records(conn, round=1, limit=20)`）拉全部一面紀錄，
   取 `questions`、`self_rating`、`impressions`，格式化成「Past first-round questions」區塊，
   總長度 cap ~1500 字元。
2. `BRIEF_PROMPT_TEMPLATE` 與 `_BRIEF_SECTIONS`（en / zh）各加一段標題與指令：
   「針對這些曾被問的題，給 2–3 句以 KB 事實為據的建議答法；沒有依據就說沒有」。
3. 不送 `interviewer` 欄位（人名不進 prompt）。

**注意**
- voice.md 已經經 `_qdrant_query` 咽喉進 context，brief 不需另讀。
- 5 筆樣本不值得做 LLM 自動覆盤；人寫、機器餵。
- 既有 brief 的注入防護段 `inj` 要涵蓋新區塊（紀錄是使用者手寫，風險低，但格式一致）。
- 測試：`tests/` 找 brief 既有測試補一個「有紀錄時 prompt 含區塊、無紀錄時不含」。

**驗收**：對現在 `interview_1` 那筆（U-Glow）重生 brief，含歷史題區塊。

---

## 5. 兩個一行修

### 5a. scheduler 探測 URL 跟 provider 走
- `scheduler.py:70` `PROBE_URL` 預設寫死 `https://api.mistral.ai/`。
- 改：`utils/llm.py` 新增 `probe_url()`：azure → `AZURE_ENDPOINT`、openai → `https://api.openai.com/`、
  mistral → `https://api.mistral.ai/`、custom → `CUSTOM_BASE_URL`；env `PIPELINE_PROBE_URL` 仍最高優先。
- **注意**：先看 `scheduler.is_online()` 對 4xx 的判定。Azure 根路徑回 404 / 401，若它只認 2xx 會誤判離線、
  日跑永遠不啟動。啟動 log 那行會順帶變成正確 host。
- 驗收：`docker logs` 啟動行顯示 azure host；`is_online()` 測試補 404 視為 online。

### 5b. geo 閘補裸國名
- 實證：source lever、company jobgether、location 為 `US` / `United Arab Emirates` / `Saudi Arabia` / `Chile`
  的 A/B 已評分列共 103 筆；`Brazil` / `India` / `Canada` 已被閘住，代表國名表存在只是不全。
- 改：`utils/geo_de.py` 國名表補 US / USA / United States / United Arab Emirates / UAE / Saudi Arabia /
  Chile / Mexico / Argentina / Australia / Singapore 等；`tests/test_geo_de` 各加一例。
- 回填：一次性腳本把既有這 103 筆標 `geo_excluded`（或 status→skipped），讓它們離開佇列供給；
  **不重評**。腳本走 `docker exec … python3 -`，不從 host 開 DB。
- **注意**：`US` 兩個字母要精確比對整個 location 字串，別用子字串（會吃掉 `Neuss`、`Husum`）。
- 驗收：回填後 `select count(*) … location='US' and status='scored'` = 0；新進 lever 裸國名列進 un-scored。

---

## 6. 測試對 `.env` 免疫（最低優先）

**目的**：`phase2_scorer.py:31`、`phase1_ingestor.py:35`、`scheduler.py:53`、`check_api.py:9` 在
import 時 `load_dotenv()`；ship 的 `docker run -v $PWD:/app` 把 `.env` 一起掛進去，使用者每加一個
env 我就得在測試 pin 一個值（已發生：`CHAT_REASONING_EFFORT`、`KB_SCORE_THRESHOLD`）。

**實作計劃**
1. 四個模組層級呼叫改成 `if not os.getenv("JOB_HUNTER_SKIP_DOTENV"): load_dotenv()`。
   dashboard 內函式層級的呼叫不動（執行期才跑）。
2. `.claude/skills/ship/SKILL.md` 與 README 測試指令加 `-e JOB_HUNTER_SKIP_DOTENV=1`。
3. 既有測試裡的 pin（`test_llm_adapter.py:40–42, 119`）可留著當雙保險。

**注意**
- 三個容器由 compose `env_file: .env` 注入，執行期不受影響。
- 只解「測試被 .env 污染」，不重構 20 多處 `load_dotenv`。

**驗收**：加了 flag 的 `docker run` 跑全套 652 測試通過；故意在 `.env` 加 `KB_SCORE_THRESHOLD=0.9`
測試仍過。

---

## 明確不做
- Azure Batch API 半價：非同步提交加輪詢的複雜度，換每月 ~$3.5。
- `reasoning_effort` 提到 medium：low 已 0/80 禁語、硬門檻全對，沒有觀察到的品質缺口。
- 全池重評、面試後 follow-up 信按鈕。
