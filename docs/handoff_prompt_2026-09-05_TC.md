# 接手提示（貼給新 session 的第一則訊息）

你接手 `/Users/toooair/job-hunter` 這個求職管線專案的下一批工作。前一個 session 已把
LLM 後端從 Mistral 換到 Azure OpenAI 並全部驗證完成，接下來要做的是一份已寫好的工作清單。

## 先讀（不要跳過）
1. `docs/worklist_2026-09-05_TC.md`：六個項目，每項有目的、實作計劃、注意事項、驗收判準，
   以及已拍板的數值（日預算 `LLM_DAILY_BUDGET_USD=2.0`、停損判準新句）。這是本次任務的規格。
2. `docs/plan_2026-09_mid_level_offer_TC.md`：整體目標與「明確不做」清單，別重做已否決的東西。
3. `CLAUDE.md` 與自動載入的 memory 索引；遇到 LLM、DB、部署相關決策先查 memory 再動手。
4. 動任何檔案前先讀該檔案相關段落。

## 執行順序與節奏
- 順序：1 成本護欄 → 5b geo 閘裸國名 → 5a 探測 URL → 2 翻譯退役（先 Step A 實驗，通過才做 Step B）
  → 4 brief 注入歷史一面題 → 3 停損判準與草稿老化 → 6 測試對 .env 免疫。
- 每個項目：先給 5 行以內的計劃讓我確認 → 實作 → 容器內跑全套測試 → commit → `/ship`。
  一個項目一個 commit（或一條 `feat/...` 分支 `--no-ff` 併回 main）。
- 每項完成後用繁體中文說：改了什麼、為什麼、影響、還沒驗證的部分。

## 硬規則（違反會造成實際損害）
- 永遠用繁體中文回覆；程式碼、commit 訊息、註解用英文。
- **絕不修改 `.env` 或任何祕密**。需要新環境變數時，把要加的那幾行原文告訴我，由我加；
  加完要 `docker compose up -d` 才生效，由你提醒我。`.env.example` 要同步，值一律虛構。
- **絕不 `git add`** gitignored 的真實設定：`config/grading_rules.md`、`config/search_targets.yaml`、
  `candidate_kb/*`。commit 前 `git status` 自查。
- **DB 只能在容器內查**：`docker exec -i -w /app job-hunter-pipeline-1 python3 - <<'EOF' … EOF`，
  容器沒有 sqlite3 CLI；**絕不從 macOS host 開 `data/jobs.db`**（WAL 會被壓壞）。
- `docker compose build` / `up -d` 之前先確認容器內沒有長任務：
  `docker exec job-hunter-pipeline-1 ls /proc/[0-9]*/cmdline` 逐一看（沒有 pgrep）。重建會靜默殺掉它們。
- zsh 開了 noclobber：寫檔用 `>|`。一個指令只放一個 heredoc（兩個會錯位）。
- commit 結尾兩行：
  `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`（依你實際模型名）
  `Claude-Session: <本 session 的 URL>`
- 測試指令（唯一的 CI）：
  `docker run --rm -v "$PWD":/app -w /app -v "$PWD"/config:/app/config:ro job-hunter-app:latest python3 -m unittest discover tests -q`
  基準 652 個測試全過。`.env` 會經 `load_dotenv()` 漏進測試程序，新增 env 變數時測試要 pin 值
  （見 `tests/test_llm_adapter.py` 的做法），這正是第 6 項要解的問題。

## 程式碼契約（別繞過）
- 所有 LLM client 只從 `utils/llm.make_client()` 建；所有 chat 呼叫走 `chat_completion()` /
  `chat_parse()`（GPT-5 家族拒絕 `temperature` 與 `max_tokens`，adapter 會自學並改寫，直接呼叫
  SDK 會把職缺標成永久 error）。模型名用 `chat_model()` / `translation_model()` / `emb_model()`。
- 暫時性失敗（429、預算超額）一律 `raise TransientAbort(...)` → exit 75 → scheduler 退避、職缺留
  un-scored。**絕不**用 `mark_error`；也絕不呼叫 `reset_errors_to_unscored`（會復活 600 多筆歷史錯誤）。
- 評分 A 門檻 76（`derive_grade`），是 Luna 刻度校準過的，不要改。
- KB 目錄有 `.kb_model` 守衛，embedding 模型不符會拒跑；`.env` 有 `KB_SCORE_THRESHOLD=0.35`。
- 現行部署：`LLM_PROVIDER=azure`，chat=gpt-5.6-luna、translation=gpt-5-nano、
  emb=text-embedding-3-small（1536 維）、`CHAT_REASONING_EFFORT=low`。

## 開工前的一件檢查
看 09-05 07:00（Europe/Berlin）第一次無人值守 Azure 日跑：
`docker logs job-hunter-pipeline-1 --since 2026-09-05T05:00:00 2>&1 | grep -E "LLM provider|Scoring failed|Marked job|TransientAbort|評分"`
開頭應有 `LLM provider: azure | chat=gpt-5.6-luna …`，且沒有 `Scoring failed` / `Marked job`。
有異常先報告，不要直接修。

## 工作風格
- 最小 diff，不順手重構。不新增依賴。
- 探測範圍太窄時，別把「沒找到」講成「不存在」。
- 有數據可查就查數據，不要憑印象；引用數字時說來源查詢。
- 遇到不確定且會影響結果的決策，停下來問我；其餘自行判斷並在報告裡標明假設。

現在請先讀上面四份文件，看完 07:00 日跑 log，然後給我第 1 項（成本護欄）的實作計劃。
