# Steps from Apple Health into Dorae-3

Apple keeps Health data on your iPhone; there's no online API for it. So a small automation in the iPhone's
**Shortcuts** app reads today's steps and saves them as a tiny text file in **OneDrive**. OneDrive syncs it to your
computer, and Doraemon reads the folder every 10 minutes (and right before the 6pm check).

Nothing else on your computer is opened up, it works anywhere, and if your laptop is off it catches up later.
It takes about 5 minutes to set up.

## Before you start

- The **OneDrive** app is installed on your iPhone and signed in to the same account as your computer.
- Doraemon looks in `OneDrive/Doraemon/health/` (on Windows: `C:\Users\<you>\OneDrive\Doraemon\health`).
  Change it with `DORAEMON_HEALTH_FOLDER` in `.env` if you'd rather use another folder.

## 1. Make the automation

1. Open **Shortcuts** → **Automation** tab → **+** (New Automation).
2. Choose **App**, pick an app you open every day (Telegram works well), tick **Is Opened**, and choose
   **Run Immediately**. Then **Next** → **New Blank Automation**.

   Why an app opening and not a time of day? Health data can't be read while the phone is locked, so a 9pm
   automation fails if your phone is in your pocket. Opening an app means it's unlocked, and it runs several times
   a day. Doraemon keeps the highest count for each day, so an earlier run never undoes a later one.

## 2. Add the actions (in this order)

1. **Find Health Samples** → tap *Type* and pick **Steps**. Add a filter: **Start Date** *is* **Today**.
   - If you wear an **Apple Watch**, add another filter: **Source** *is* your iPhone (or your Watch). Both record
     steps, and adding them up would count your walk twice.
2. **Calculate Statistics** → **Sum** of *Health Samples*.
3. *(optional, for active calories)* another **Find Health Samples** with *Type* **Active Energy** and
   *Start Date is Today*, then **Calculate Statistics** → **Sum**.
4. **Format Date** → *Current Date*, Format **Custom**, `yyyy-MM-dd`.
5. **Text** → type the line below, inserting the variables (tap the blue variable bubbles):

   ```
   [Formatted Date] steps [Statistics]
   ```

   With active calories: `[Formatted Date] steps [Statistics] active [Statistics 2]`.
6. **Save File** → turn **Ask Where to Save** off, pick the **OneDrive** location and the `Doraemon/health`
   folder, set *Subpath* to `steps-[Formatted Date].txt`, and turn **Overwrite If File Exists** on.

## 3. Check it

- Tap the automation's ▶ (or open the app you chose). A file like `steps-2026-10-19.txt` appears in
  `OneDrive/Doraemon/health`, containing something like `2026-10-19 steps 8423 active 412`.
- On your computer, the **Fitness** page shows today's steps within about 10 minutes, and Dorae-3 answers
  "how many steps today?".

## Good to know

- Doraemon reads lines like `2026-10-19 steps 8423`, `2026-10-19,8423,412` or `Steps: 8,423` (thousands separators
  and day-first dates are fine). A line without a date takes it from the file's name.
- Files can stay in the folder; each day keeps its highest count. Delete old ones whenever you like.
- Paid apps such as *Health Auto Export* can write the same kind of file on a schedule, if you'd rather not use
  Shortcuts.
