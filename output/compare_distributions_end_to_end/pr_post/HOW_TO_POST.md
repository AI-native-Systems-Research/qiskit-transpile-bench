Post the contents of [pr_comment.md](pr_comment.md) in the PR description or a comment. The four PNGs are in [images/](images/). The relative image paths work in a local Markdown preview; replace them with uploaded image URLs before posting.

1. Open `pr_comment.md` as plain text and copy its contents into the PR editor. Do not wrap it in a code block.
2. Upload the four PNGs to the editor by dragging them into the text box. Wait for each upload to finish; GitHub inserts an image embed with its hosted URL. See [GitHub's attachment instructions](https://docs.github.com/en/get-started/writing-on-github/working-with-advanced-formatting/attaching-files).
3. For each image, copy its uploaded URL into the matching `![...](images/...)` line, replacing only the path inside parentheses. If GitHub inserts an HTML `<img>` tag, use its `src` URL. Delete the extra embed inserted by the upload so each image appears once.
4. Check Preview: four graphs should appear, the tables should render, and the Family breakdown should expand. Make sure no image link still starts with `images/`.
5. Save the description or submit the comment.

| File to upload | Graph |
| --- | --- |
| `images/01_depth_summary.png` | D2 summary |
| `images/02_gate_count_summary.png` | N2 summary |
| `images/03_depth_vs_decay.png` | D2 case means vs decay |
| `images/04_gate_count_vs_decay.png` | N2 case means vs decay |

The PNGs are the original graphs used on pages 5-8 of [report_presentation.pdf](../report_presentation.pdf), copied at their original resolution. Quantile/SD conventions and the confidence-interval note were checked against [report.md](../report.md).

These files are ready for manual posting; nothing has been uploaded or posted.
