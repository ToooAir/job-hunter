# Plan：全德國中階職缺 → 一份能轉簽的正式 offer（2026-09-02）

## 0. 目標與成功判準

- 目標：拿到一份德國境內正職 offer，能轉藍卡、§18b 或至少 Folge-Chancenkarte。
- 薪資：≥ €45,934.20（2026 藍卡短缺職業門檻，IT 適用）即可；一般門檻 €50,700。
  §18b（45 歲以下）無薪資門檻，但需 anabin 認證學位 + 工作內容對得上學位。
- 範圍：全德國、中階或以下職稱、英語或 de_plus 環境（A2 德語打不開 de_required）。

## 1. 漏斗現況（2026-04 至 09-01）

| 階段 | 數字 | 換算 |
|---|---|---|
| 投遞 | 682 | 近兩月 170–230/月 |
| 一面（peak_stage） | 9 | 1.3% |
| 一面 → 二面 | 0 / 8 已定案（1 筆進行中） | **0%** |
| Offer | 0 | |

- 9 筆一面共同特徵：小型公司或新創（4 人團隊等）、visa=open、標題無資歷字眼或 AI/Python 標籤、
  來源 wearedevelopers 6/188（3.2%），BA + jobware 92 投 0 面試。
- 5 筆有面試紀錄，自評 4–5 分，技術段落反饋好；問題集中在「為什麼來德國、簽證與德語進度、
  搬遷意願、high agency」。全部在一面後被拒。
- 供給端：佇列 0、over_budget 36（全 company-cap）、31 張草稿待投。近 30 天 A/B 新公司 336 筆
  拆解：境外 remote 197、B 低於 65 共 109、已有草稿 22、真正可投 **8**。管線沒有漏，池子就是薄。
- 拒信速度：7 月起 199 筆拒信中位 6 天、67% 在 7 天內，各來源一致 → 篩選層共因（簽證/德語/履歷），
  不是來源問題。

## 2. 這份 plan 對目標真的有幫助嗎（誠實評估）

offer 期望值 = 投遞數 × p(一面) × p(一面→二面) × p(二面→offer)。

- **工程項目只能動「投遞數」**（供給）。把 116 筆通用後端從 C 撈回 B、補 arbeitnow 小城、
  BA 擴 bundesweit，都是加抽數。以 1.3% 估，每多 100 抽多 1.3 個一面。
- **p(一面) 不會因此上升，可能微降**：新增的池子是「通用後端、無 AI 標籤、Mittelstand 為主」，
  而 9 筆一面多數來自 AI/Python 標籤的新創。抽卡框架下這仍划算（乾旱期主因是抽不到），
  但不要期待轉換率變好。
- **最大的洞在 p(一面→二面) = 0/8，管線碰不到它**。若這一段不動，供給再多只是產生更多會死在
  一面的面試。這一段的槓桿是面試覆盤與敘事（簽證、德語、搬遷、為什麼是這家小公司），不是程式碼。
- **「全德國」和數據有張力**：8/9 一面在柏林/漢堡/慕尼黑，唯一例外（Bernkastel-Kues）也是
  wearedevelopers 來的。小城 Mittelstand 池 de_required 比例更高、可觸及率 ~20%、
  BA 快拒 84% ≤ 7 天。擴量前先讓 regex 閘把 de_required 在 LLM 前攔掉，否則是花錢買拒信。
- **效率項目（regex 閘、BA bonus）對 offer 是零貢獻**，價值是省錢省時，並讓擴量不失控。

結論：工程 plan 是**必要但不充分**。供給 8 筆/30 天確實是眼前的硬瓶頸，Phase 1–2 該做；
但 Phase 0 的三件非工程事（anabin、面試覆盤、居留身分敘事）對「拿 offer」的期望值更高，
應排在所有程式改動之前或同時進行。

## 3. 分階段

### Phase 0：非工程，1–2 天，先做

1. ~~anabin 確認~~ **已結案（2026-09-03 使用者說明）**：Chancenkarte 當初就是走學歷路線核發，
   anabin 認證已過。藍卡與 Chancenkarte 的差距只剩一份合約；薪資門檻 IT 短缺職業 €45,934。
   Answer Panel 底線已設 52,000。
2. **8 筆一面拒絕覆盤**：5 筆有紀錄。找共同題（簽證/德語/搬遷/動機）與回答方式，
   寫成 candidate_kb/voice.md 的一段（生成端與驗證端共用，存檔即生效）。
3. **居留身分敘事進 CL 與 profile**：一句話固定出現在 CL Para 1 或 profile 事實：
   already in Germany on Chancenkarte, eligible to start, in-country switch to Blue Card,
   no relocation needed。目標是砍掉篩選層最常見的 knockout。
4. 投掉 31 張待投草稿（12 張 BA 草稿 08-29 起、14 張 arbeitnow 最舊 08-12）。草稿會腐爛。

判準：Phase 0 完成前，不開 Phase 2 擴量。

### Phase 1：供給，小 diff，1 天

| 項目 | 改動點 | 測試 | 風險 |
|---|---|---|---|
| 1a 重錨 grading_rules | `config/grading_rules.md`：Core = Python/Node/REST/SQL/Docker/CI/CD/雲；AI 改加分項並明寫 "a role that does not require AI/LLM is not a gap"；"seniority aligns" → "candidate meets or exceeds the requirement" | 無單元測試；用 1b 的重評結果驗證 | B 池變寬稀釋草稿品質，靠 company-cap 與排序頂住 |
| 1b 目標式回溯重評 | 新 script `scripts/rescore_generic_backend.py`：只挑 status=scored、fit_grade=C、match_score 50–59、非 de_required、JD 含 python/node/django/fastapi/typescript、德國 location 的 ~116 筆，呼叫 `score_single_job` | dry-run 先印清單；跑後比對 B 升級數 | 116 次 LLM 呼叫，一次性；**不做全池重跑** |
| 1c arbeitnow 城市過濾 | `phase1_ingestor.py` scrape_arbeitnow：`loc_match` 改用 `utils/geo_de.is_germany_location`，保留 remote_match | 加測試：`Aachen`/`Ulm` 進、`Madrid`/`Remote — non-EU` 不進 | 零額外請求；非 remote 小城職缺會多進池，也多進評分（配合 Phase 2 閘） |

判準（21 天窗）：`build_queue` 每週可投新公司數從 8/30 天升到 ≥ 15/週；否則供給不是這裡的槓桿。

**Phase 1 狀態（2026-09-02 完成）**
- 1a 已改（config/grading_rules.md + .example）：AI 改加分項、通用後端明寫落在 70–84、overqualified 不扣分。
- 1b 已跑 `scripts/rescore_generic_backend.py`：116 筆重評 → A 8 / B 29 / C 79（32% 翻身），0 失敗。
  翻身者進 build_queue 的 needs_recheck 31 筆，等下一輪 ats_scan JIT 驗活後入佇列。
- 1c 已改 phase1_ingestor `_arbeitnow_location_ok`（+ tests/test_arbeitnow_location.py）：
  同一次抓取實測 83 → 91 筆通過（+8 非 remote 小城）。
- 容器為 docker cp 快測版，image 未 rebuild；600 測試綠。

### Phase 2：效率與擴量，1–2 天

| 項目 | 改動點 | 測試 | 風險 |
|---|---|---|---|
| 2a de_required regex 前置閘 | `phase2_scorer.py` pre-flight，**在翻譯之前**；新共用 `utils/lang_req.py`：德語錨定 pattern（deutsch/german 60 字內出現 C1/C2/verhandlungssicher/fließend/sehr gut/muttersprach/fluent/native），前後 60 字有 von Vorteil/plus/preferred/nice to have/ideally 則放行；命中直接寫 jd_language_req=de_required、fit_grade=C、status=scored、top_3_reasons 註明 rule-gated | 回歸測試：`English C1` 不攔（實測誤殺 23 筆）、`Deutsch B2 von Vorteil` 不攔、`verhandlungssichere Deutsch` 攔 | 實測：攔 1,499/2,546 de_required，非 de_required 誤攔 75/5,000（1.5%），且 A/B 命中的 16 筆多數是 LLM 標錯。省 ~25% scorer 呼叫 + 同比例翻譯 |
| 2b 拿掉 BA +5 | `SOURCE_BONUS` 刪 bundesagentur（一行）；不回溯 | 現有測試 | BA 的 A 級掉一截，本來就是灌水；37 投 19 拒 0 面試 |
| 2c BA bundesweit + 翻頁 | `config/search_targets.yaml` bundesagentur.locations 改為空字串（全國）+ `page` 1..N；`phase1_ingestor.py` 加 page 迴圈與 `seen_not_stored` 帳本沿用 | 手動跑一次看 maxErgebnisse 與新增數 | 抓取時間上升（原 31m 全量），detail 呼叫多；**必須在 2a 之後開** |
| 2d wearedevelopers 頁數 | 先驗 API 第 4–5 頁是否有資料，再決定 max_pages 3→5 | 手動 probe | 唯一有面試轉換的來源，值得先驗 |

判準：scorer LLM 呼叫量下降 ≥ 25%、誤攔 ≤ 2%（抽 50 筆人工看）；BA 擴量後 21 天內佇列來自 BA
的新公司數與快拒率一起看，快拒 ≥ 80% 就縮回城市模式。

**Phase 2 狀態（2026-09-02 完成）**
- 2a 已做：`utils/lang_req.py` + `tests/test_lang_req.py`，掛在 phase2 pre-flight 的翻譯之前；命中直接
  update_score 成 C/de_required/score 0，top_3_reasons 以「rule-gated:」開頭可 grep 稽核。全庫回測：
  攔 1,376/2,548 de_required（54%），非 de_required 誤攔 53/5,571（0.95%），A/B 命中 14 筆全是
  「verhandlungssichere/fließende/sehr gute Deutschkenntnisse」被 LLM 標成 de_plus 的案例。
  修過的坑：`\bgerman\b`（否則 "based in Germany" 配到 native）、拿掉 proficiency（"German
  language proficiency (B1 or above)" 不是硬需求）、span 內含 English/Englisch 一律不算。
- 2b 已做：SOURCE_BONUS 刪 bundesagentur +5（不回溯）。
- 2c 已做：BA `nationwide: true` + `max_pages: 3`（省略 wo 即全國，wo="" 是 400；Software Engineer
  全國 1,314 筆 vs 漢堡 84）；分頁到 maxErgebnisse 或 max_pages 為止；5 個新測試。
- 2d 變成事故處理：**wearedevelopers 從 2026-08-17 起 0 筆**（wad-api /v2/jobs/search 對任何查詢回
  200 + 空陣列，log 每天「新增 0 筆，略過 0 筆」16 天沒人看見）。站方改成 Hotwire SSR，並依
  /agents.md 提供 Markdown 版：`/jobs.md?country=DE&q=<kw>&page=N`（每頁 24）與
  `/jobs/ext/<id>-<slug>.md`（含 JD、Apply）。scraper 全部改寫（`_wad_parse_listing`/
  `_wad_parse_detail`/`_wad_md`，`tests/test_wearedevelopers_md.py` 8 個測試），舊 `_wad_jd`/
  `_wad_safe_get` 刪除；ats_scan 對舊格式 URL 仍走舊 detail API 做 liveness（新格式 URL 待接
  `.md` 404 當 liveness，未做）。config max_pages 3→5、per_page 移除。
  **回填實跑（09-03 00:58 完成）**：新增 1,866 筆／1,129 家公司、1,865 筆有 apply_url、0 筆短 JD、0 警告，73 分鐘。
  un-scored 池升到 5,876，隔日評分會很長（de_required 閘先攔一批）。
- 618 測試綠；容器為 docker cp 快測版，**image 未 rebuild、未 commit**。
- 教訓：來源「新增 0 略過 0」連續多天必須告警，不能只靠人看 log。

### Phase 3：劃掉（2026-09-03）

使用者拍板不做：佇列為 0 時排序無效；eu_only 標籤把「需工作許可」誤歸一類是噪音；
簽證在面試桌上談，不是 JD 標籤能預判。原構想留檔：


- visa_restriction 拆兩級（eu_only 明確排外 vs work_permit 只要求工作許可）+ 佇列 demote。
  **只在佇列長度 > budget 時才有意義**（現在佇列 0，排序改了等於沒改）。觸發條件：連續兩週
  over_budget 裡出現非 company-cap 的職缺。
- 面試率分來源進排序 tie-breaker：n=9 撐不起，等 ≥ 20 筆一面再談。

## 4. 預註冊停損（防事後合理化）

- Phase 1+2 上線後 6 週：投遞 ≥ 250 且一面 ≤ 2 → 供給不是槓桿，停止管線投資，轉攻 p。
- 一面 ≥ 3 但二面仍 0 → 問題在面試層，停止管線投資，全力覆盤面試。
- 任一階段發現 regex 閘誤攔 > 2% → 關閘回 LLM 判定。

## 5. 明確不做

- 全池重評（成本高，只有 50–59 那層有翻身空間）。
- 履歷逐職缺客製（07-17 已否決，A≈B 零訊號）。
- Remote — EU 納入（非德國雇主拿不到德國工作許可，9/9 放棄實證）。
- 薪資過濾（中階後端市場價已高於門檻）。
- 通用表單 matcher / 瀏覽器自動送出（已否決）。
