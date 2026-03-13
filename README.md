# Gold (XAU/USD) Trading Bot

This is an automated trading bot specifically for Gold (XAU/USD). It connects to the Tiingo service for real-time FX data, uses an advanced, multi-indicator strategy to generate trading signals, and manages them through a Firebase Realtime Database.

## Features

- **Advanced Trading Strategy**: Combines a long-term EMA (200-period on 15m chart) for trend direction with a MACD crossover and RSI filter for precise entries. This is tailored for Gold's unique market behavior.
- **Dynamic Risk Management**: Automatically calculates Stop-Loss (SL) and Take-Profit (TP) levels based on the Average True Range (ATR). The multipliers are set higher to give Gold's volatility more room to move.
- **Real-time Price Monitoring**: Uses Tiingo's WebSocket for live price tracking and signal management.
- **Firebase Integration**: All signals, active trades, and trade history are logged to a Firebase Realtime Database for easy monitoring.
- **Configurable**: All strategy parameters are easily configurable within the main `Gold_Bot.py` file.

## Setup and Installation

### 1. Clone the Repository
```bash
git clone <your-repo-url>
cd <your-repo-folder>
```

### 2. Install Dependencies
```bash
pip install -r requirements.txt
```

### 3. Configure Environment Variables

For secure deployment on platforms like Render or Railway, you must use environment variables. Do **not** commit your credential files or API tokens.

- **`TIINGO_API_TOKEN`**: Your API token for the Tiingo FX service.
- **`FIREBASE_DB_URL`**: The URL of your Firebase Realtime Database.
- **`FIREBASE_CREDENTIALS_PATH`**: The local path to your Firebase service account JSON file.

**Important for Deployment:** On a hosting service, it is more secure to store the content of the JSON file in a single environment variable. This bot is already configured to look for `FIREBASE_CREDENTIALS_JSON`. If this variable is set, it will be used instead of the file path.

### 4. Run the Bot
```bash
python Gold_Bot.py
```

## GitHub Updates

To push your local changes to GitHub, use the following commands in your terminal:

1. **Stage your changes**:
   ```bash
   git add .
   ```
2. **Commit your changes**:
   ```bash
   git commit -m "Describe your changes here"
   ```
3. **Push to GitHub**:
   ```bash
   git push origin main
   ```
   - git add .
   - git commit -m Update
   - git push

## Common Git Issues & Solutions

### 1. "Updates were rejected because the remote contains work that you do not have locally"
This happens if someone else updated the repository or you made changes on the GitHub website directly.
**Solution**: Pull the latest changes first, then push.
```bash
git pull origin main
# If there are conflicts, resolve them, then:
git add .
git commit -m "Merge remote changes"
git push origin main
```

### 2. "Authentication failed"
This happens if your GitHub credentials are incorrect or your token has expired.
**Solution**: Use a Personal Access Token (PAT) instead of your password if prompted. You can generate one in GitHub Settings > Developer Settings > Personal Access Tokens.

### 3. "Permission denied (publickey)"
This happens if your SSH key is not added to your GitHub account.
**Solution**: Use the HTTPS URL for your repository instead of SSH, or add your SSH key to GitHub.

### 4. How to undo a mistake before pushing?
If you committed something by mistake but haven't pushed yet:
```bash
git reset --soft HEAD~1
```
This will undo the commit but keep your changes staged so you can fix them.
