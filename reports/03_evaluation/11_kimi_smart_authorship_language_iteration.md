# Kimi-Smart 语言一致性与模板化 authorship 迭代

日期：2026-08-27

## 失败案例

Slides-Align `market_analysis` 的 Kimi-Smart 人评 rank 为 4，但旧
`finished-deck-v8@8.0` 得到 `84.129249 / PASS`，在七份切片的 Harness 排序中位列第一。
它的单页布局规整，但 26 页存在两类旧指标覆盖不足的问题：

- 第 1/2/3/26 页使用中文外壳，22 页正文几乎全英文，属于未声明的部分本地化；
- 7 个章节页使用完全相同轮廓，大量正文页复用等权卡片、小图标、通用总结句和匿名案例，
  形成系统性模板化 authorship 风险。

旧 `authorship_specificity=0.679817` 已提供轻微扣分，但规则会奖励长文本和任意数字，
无法区分 `Competitor A / ?/10 / a SaaS firm` 等占位信息；六个视觉 VLM criterion 也会把
卡片和图标的整洁一致直接当作正向视觉质量。

## v8.2 合同

- `language_consistency`：独立 4% primary owner；从 content structure 的 25% 中划转，
  显式双语、专业缩写和品牌名不扣分。
- `authorship_specificity_v2`：仍占 12%，不另加第二个 “AI 分”。
- 规则检测跨页同构轮廓、机械卡片/图标、占位案例和公式化文案，置信度保持较低。
- 新增第七个 v8-only 跨页 VLM criterion；历史 v6/v7 六维常量保持不变。
- authorship 规则占 30%、同构念 VLM 占 70%，只产生一个公式入口；旧 metric ID 为不计分别名。
- language/authorship observation 不进入 functional hard gate，避免 additive 扣分后再乘 0.5。
- 新 VLM 采用风险页、封面、结尾和固定探索样本，不再只看等距的整洁页。

“AI 风格”在合同中不表示生成来源推断，只表示可观察的 formulaicity/specificity。
极简、黑白、暗色、统一品牌系统、一个合理的 taxonomy/checklist/process 页面和功能性图标
都不是缺陷。

## 真实重放

Profile：`finished-deck-v8@8.2`<br>
Run：`run-5bc786f0-ba71-4808-a4f4-ad97e2cee6d9`

| 项目 | 结果 |
|---|---:|
| language consistency | 0.783654 |
| deterministic authorship rule | 0.443109 |
| qwen3.8 authorship VLM | 0.36 |
| fused authorship specificity v2 | 0.384933 |
| base/full score | 76.441726 |
| decision | REVIEW |
| errors | 0 |

authorship VLM 使用页码 `1,26,23,24,22,15,4,8`，识别出
`repeated_template_silhouette`、`mechanical_cardization` 和
`weak_focal_claim_specificity`，影响页为 `1,4,8,15,22,23,26`。Qwen 置信度为 0.90，
没有触发 GLM fallback；该调用使用 36,682 token。
整份 run 共记录 237,122 token；typography 与 imagery 因规则/模型分歧分别触发一次
同构念 GLM 复核，authorship 本身未升级。

为区分指标设计与模型切换影响：若保留旧六个视觉分，只替换 language/authorship 合同，
反事实总分为 `80.181726`，相对旧版下降 `3.947523`；实际 `76.441726` 与该反事实的
其余 `-3.74` 主要来自 Qwen3.8 本轮对 palette、typography 和 visual communication 的不同判断。
不能把全部降幅都归因于 authorship 新指标。

## 审计产物与限制

- HTML：`var/datasets/slides_align_sample/report_qwen38_glm53_kimi_smart_v82/index.html`
- report：`var/datasets/slides_align_sample/report_qwen38_glm53_kimi_smart_v82/kimi_smart_rank_4.report.json`
- comparison：`var/datasets/slides_align_sample/report_qwen38_glm53_kimi_smart_v82/comparison.json`
- 运行级哈希链：有效

该单例验证构念方向，不用于拟合 4%/12% 权重或生产阈值。第 20 页图片内部仍包含
“This slide is 100% editable”模板残留和裁断标题，但对象树规则不可见，且本轮 authorship
风险样本未包含该页；仍需 OCR/可见 template-residue 原子审计。七份真实切片的相关性与
模型重复运行方差应在后续固定代码版本上重放。
