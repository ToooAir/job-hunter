 ---
  1. 評分 JD 未截斷（高影響）

  score_jobs() 裡 effective_jd 傳給 build_prompt 時沒有截斷：

  effective_jd = job.get("translated_jd_text") or job["raw_jd_text"]
  system_prompt, user_prompt = build_prompt(jd_text=effective_jd, ...)

  raw_jd_text 沒有 [:N] 限制，長 JD 可能 3000–5000 tokens。其他函式（brief、CL regen）都有截 6000
  chars，唯獨評分沒有。
  建議：加 [:6000]，每筆可省 ~300–1,500 tokens。

  ---
  2. regenerate_cover_letter 有死碼

  _rules_path = Path(__file__).parent / "config" / "grading_rules.md"
  grading_rules = _rules_path.read_text(encoding="utf-8")  # 讀了
  # ... 但 grading_rules 從未插入 prompt，直接被丟棄

  讀了 grading_rules.md 但沒用。刪 3 行。

  ---
  3. RAG top_k 全部固定為 5

  retrieve_context 預設 top_k=5，所有 on-demand 函式都沿用。

  - 評分：需要 5 chunk，因為要覆蓋技術棧、經歷、簽證等不同維度 — 合理
  - interview brief：只需「你的相關亮點」，3 chunk 夠用，省 ~400–600 tokens
  - CL regen：同上，3 chunk 足夠

  建議：brief 和 CL regen 的 retrieve_context 改 top_k=3。

  ---
  4. 評分 + CL 合一（結構性問題）

  目前一次 LLM call 同時產出：分類欄位 + match_score + top_3_reasons + cover_letter_draft。

  CL 輸出佔了 300–600 tokens，但 C 級職缺你不會用。若拆成兩個 call：
  - Call 1：只輸出 JSON 分類（~80 tokens output）
  - Call 2：只對 A/B 級生成 CL

  假設 C 級佔 40%，可省 40% 的 CL 輸出 token。但 Mistral 1 RPS 下 A/B 級每筆要多 1 秒，得權衡。

  ---
  5. 簽證分析傳整份 JD（中影響）

  visa_checker 把完整 JD（6000 chars）傳給 LLM，但 regex pre-scan
  其實已經把相關關鍵字抓出來了。可以只傳包含關鍵字的上下文段落，而非整份 JD，估計可省 ~1,000–2,000
   tokens。

  ---
  6. 翻譯截斷過寬

  _translate_to_english 截到 8000 chars（~2,000 tokens）。德文 JD 的關鍵資訊幾乎都在前半段，截到
  4000 chars 就夠，省 ~500 tokens。

  ---
  7. grading_rules.md 全文注入每筆評分

  ~500–700 tokens 的規則文件在 100 筆職缺裡重複 100 次。這個無法避免（除非用支援 system prompt
  caching 的 provider），但可以確保 grading_rules.md 保持精簡，不要加廢話。

