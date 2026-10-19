# Steps from Apple Health into Dorae-3

Apple keeps Health data on your iPhone; there's no online API for it. So a small automation in the iPhone's
**Shortcuts** app reads today's steps and saves them as a tiny text file in **iCloud Drive**. iCloud for Windows
syncs it to your computer, and Doraemon reads it every 10 minutes (and right before the 6pm check).

Nothing on your computer is opened up, it works anywhere, and if your laptop is off it catches up later.
It takes about 10 minutes the first time.

## 1. On your computer: iCloud for Windows

1. Install **iCloud** from the Microsoft Store and sign in with the same Apple ID as your iPhone.
2. Tick **iCloud Drive** and press **Apply**. A folder `C:\Users\<you>\iCloudDrive` appears in File Explorer.

That's all Doraemon needs: it already watches `iCloudDrive\Shortcuts\Doraemon\health` (where the Shortcut below
saves) and `iCloudDrive\Doraemon\health`. The **Fitness** page shows which one it found.

## 2. On your iPhone: the automation

1. Open **Shortcuts** → **Automation** tab → **+** (New Automation).
2. Choose **App**, pick an app you open every day (Telegram works well), tick **Is Opened**, and choose
   **Run Immediately**. Then **Next** → **New Blank Automation**.

   Why an app opening and not a time of day? Health data can't be read while the phone is locked, so a 9pm
   automation fails if your phone is in your pocket. Opening an app means it's unlocked, and it runs several times
   a day. Doraemon keeps the highest count for each day, so an earlier run never undoes a later one.

## 3. Add the actions (in this order)

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
6. **Save File** → turn **Ask Where to Save** off, set the path to `Doraemon/health/steps-[Formatted Date].txt`
   (it goes into iCloud Drive's **Shortcuts** folder), and turn **Overwrite If File Exists** on.

## 4. Check it

- Tap the automation's ▶ (or open the app you chose). In the **Files** app, *iCloud Drive → Shortcuts → Doraemon →
  health* now has a file like `steps-2026-10-19.txt` containing `2026-10-19 steps 8423`.
- On your computer it appears under `iCloudDrive\Shortcuts\Doraemon\health` within a minute or two, and within
  about 10 minutes the **Fitness** page shows today's steps and Dorae-3 answers "how many steps today?".
- If the Fitness page says *not there yet* for every folder, look in File Explorer for where the file landed and put
  that folder in `.env` as `DORAEMON_HEALTH_FOLDER=...` (several folders can be separated by `;`).

## Good to know

- Doraemon reads lines like `2026-10-19 steps 8423`, `2026-10-19,8423,412` or `Steps: 8,423` (thousands separators
  and day-first dates are fine). A line without a date takes it from the file's name.
- Files can stay in the folder; each day keeps its highest count. Delete old ones whenever you like.
- iCloud for Windows may keep new files online-only until they're opened; Doraemon opening them downloads them.
  To keep them on the computer, right-click the `Doraemon` folder → **Always keep on this device**.

## Using OneDrive instead

Install the OneDrive app on your iPhone, and in step 3.6 turn **Ask Where to Save** on and pick the
`Doraemon/health` folder in **OneDrive** (with Overwrite on). Doraemon also watches `OneDrive\Doraemon\health`,
so nothing else changes.
