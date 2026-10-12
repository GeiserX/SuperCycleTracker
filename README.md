<p align="center">
  <img src="docs/images/banner.svg" alt="SuperCycleTracker" width="100%">
</p>

<h1 align="center">SuperCycleTracker</h1>

<p align="center">
  <a href="https://github.com/GeiserX/SuperCycleTracker/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/GeiserX/SuperCycleTracker/ci.yml?style=flat-square&label=CI" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/GeiserX/SuperCycleTracker?style=flat-square" alt="License"></a>
</p>

SuperCycleTracker is a menstrual cycle tracker that lives in a Telegram group. It asks on the days a period is likely, logs flow, pain and symptoms from a plain reply, learns each person's cycle to predict the next one, and keeps every record in a private git repo you own. Built for one household; the bot talks Spanish to its users while the code and docs are English.

## Features

- Reminds her on the expected day, based on her past cycles.
- Checks in daily while the period lasts, up to 7 days or until she says it stopped.
- Logs flow (light, medium, heavy), pain from 0 to 10, symptoms and free text from one reply.
- Predicts the next period from the logged history.
- Nags when a day was missed, and accepts late entries for forgotten days.
- Imports an Apple Health export to seed the history.
- Answers in private chat always, and in the group only when mentioned or replied to.
- Makes every string translatable; English and Spanish ship.
- Keeps the data in a private git repo, never in the public app.

## Quick start

Status: the first release lands once the bot layer it depends on is published. Until then this repo holds the design.

## License

[AGPL-3.0-or-later](LICENSE)
