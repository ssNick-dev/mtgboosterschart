# MTG Booster Value → Discord (daily, no always-on server)

A GitHub Actions job automatically posts **two aligned Discord tables**, one for **Play Boosters** and one for **Collector Boosters**. After the first run it **edits the same webhook messages** rather than sending new messages. Uses **real sealed and singles market prices** and **modeled pull distributions**; EV/median are *estimates*, not observed pack-opening returns.

**Cost:** normally **$0/month** within GitHub Actions Free-plan minutes. Runs once daily. No bot token or hosting subscription required.

## 5-minute setup

1. **Create a Discord webhook:** Open your Discord server, make a `#mtg-booster-values` text channel, go to **Edit Channel → Integrations → Webhooks → New Webhook**; choose that channel and **Copy Webhook URL**. Keep the URL private. A webhook can edit its own messages, not your personal pasted messages.
2. **Create a PRIVATE GitHub repository**, e.g. `mtg-booster-values`. The repository must have GitHub Actions enabled.
3. **Upload this project to the repository's root** (not inside an extra enclosing folder). Include the **hidden `.github/workflows/daily.yml`** file and the `state/message_ids.json` file. A reliable Windows approach is to clone the empty repository using **GitHub Desktop**, copy the unzipped project contents (including `.github`) into the cloned folder, commit, and push.
4. In the GitHub repository, open **Settings → Secrets and variables → Actions → New repository secret**. Name: `DISCORD_WEBHOOK_URL`. Value: the complete Discord webhook URL from step 1. **Never paste the webhook in code, Discord, or a public issue.**
5. Under **Settings → Actions → General → Workflow permissions**, ensure **Read and write permissions** are enabled (so Actions can save the two message IDs to `state/message_ids.json`). If your organization restricts permissions, an admin may need to allow this.
6. Open **Actions → Update MTG Booster Rankings → Run workflow** on branch `main` (or your default branch). View logs if it fails. On success, it posts the two leaderboard messages; subsequent runs edit them. You may **pin** the two webhook posts manually in Discord.

The workflow automatically runs daily at **03:37 UTC** (about **8:37 PM PDT / 7:37 PM PST**, the previous calendar day). GitHub scheduling can be delayed during peak load.

## How numbers are calculated

- **Current card values:** MTGJSON `AllPricesToday` → exact card UUID and correct foil/nonfoil finish → *TCGplayer paper retail* prices.
- **Booster composition / rarity odds:** MTGJSON's weighted booster configurations and weighted card sheets.
- **Single-pack sealed price:** TCGCSV TCGplayer **marketPrice**, strictly matched to MTGJSON's actual `tcgplayerProductId`. Boxes, bundles, sample, sleeved, omega, multipacks are excluded.
- **EV** = weighted expected total of the card market values. **ROI** = EV divided by sealed individual-pack market price. **TYP** = simulated median of 6,000 pack draws.
- **Token, ad, helper and art-card slots** are modeled as worth $0. **Color balancing is not explicitly simulated**, so especially the median is an approximation. This can differ from detailed proprietary collation or actual pack-opening results. Booster sheet models and pull rates can be incomplete or estimated.
- **Data quality:** if >3% of non-token card-slot probability lacks a price for the correct finish, the set is excluded. Bad/stale upstream data aborts the workflow and **leaves Discord messages unchanged**. A table requires at least three qualifying products, so low coverage cannot masquerade as a comprehensive ranking.
- Only **released** sets from **2024 onward** are considered. The tool discovers new sets from MTGJSON on future daily runs, but will only include them if there is a real single-booster TCGplayer ID, a real market price and matching booster config. No made-up prices, no auto-guessed booster odds.
- Market value is not cash-out value: shipping, transaction fees and taxes are excluded. No guarantee of profit.

**Sources:** [MTGJSON](https://mtgjson.com/), [TCGCSV](https://tcgcsv.com/docs). Neither source guarantees data coverage. The script does not scrape TCG Balance or invent any current values.

## Test locally (optional)

Python 3.12+:

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python tracker.py --dry-run
```

Dry-run downloads real current data and writes `output/play.txt`, `output/collector.txt`, and `output/data.json` **without posting to Discord**. (It may exit without output if the live data do not meet the quality gates.)

To run locally against Discord, set `DISCORD_WEBHOOK_URL` in your environment, then use `python tracker.py`. On GitHub the workflow does this automatically. `state/message_ids.json` stores only Discord message IDs, **not** the webhook secret.

## Customize

- Edit `--min-year 2024`, `--top 12`, `--samples 6000` in the GitHub workflow's Run step command (`python tracker.py --top 15`, for example). Avoid >15 due to Discord's **2,000-character limit**.
- Edit cron in `.github/workflows/daily.yml` to change the UTC time.
- To change the Discord channel, create a **new webhook in that channel**, change the GitHub secret, **reset `state/message_ids.json` to `{}`**, and manually delete the old webhook posts if desired. If you only change the webhook secret, the old message IDs will not be editable by the new webhook.
- Re-running the same workflow will update the two posts, not spam the channel, provided the `state/message_ids.json` file is committed correctly.

## Troubleshooting

- **No packs qualified:** Not enough sets have both a matched pack price and complete enough booster/finishing data. Review the Actions log for specific skipped products. This is a safety feature, not a reason to fill missing data with estimates labeled as fact.
- **No sealed market prices:** Exact MTGJSON product IDs might not be present in the queried TCGCSV groups. Review logs; add TCGplayer group IDs to `derive_groups` if your set is listed in a different group. No fuzzy fallback is deliberately used.
- **403 when saving IDs:** Grant Actions read/write repository permissions; otherwise GitHub can't save the message IDs and the next run could duplicate posts.
- **Old posts not edited:** If the webhook URL changed or posts were deleted, the tracker will recreate posts only when Discord returns 404. Reset `state/message_ids.json` if necessary.
- **No Discord updates:** Data snapshot may be stale, or one of the two tables has fewer than three qualifying sets; the script deliberately fails before editing anything.
- **Rate limit:** TCGCSV groups are fetched gently at ~4–5 requests/s; this is intended as one daily job, not a continuously polling service.

**Security:** A Discord webhook URL is a credential. Store it only in GitHub Actions repository Secrets. Use a private repository and never commit `.env` secrets. If leaked, delete and recreate the webhook in Discord.
