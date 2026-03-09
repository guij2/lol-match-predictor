@echo off

echo Activating virtual environment...
REM The 'call' command executes the activate script and then returns 
REM to this script, keeping the environment variables (like the updated PATH)
call .venv\scripts\activate

echo.
echo ===== ETAPA 1/3: Coletando PUUIDs de jogadores ranked =====
python pythoncode/matchIDscraper.py
if %ERRORLEVEL% neq 0 (
    echo ERRO na etapa 1! Abortando.
    pause
    exit /b 1
)

echo.
echo ===== ETAPA 2/3: Coletando Match IDs a partir dos PUUIDs =====
python pythoncode/collect_match_ids.py --output D:/Data/MatchIds.csv
if %ERRORLEVEL% neq 0 (
    echo ERRO na etapa 2! Abortando.
    pause
    exit /b 1
)

echo.
echo ===== ETAPA 3/3: Coletando dados de timeline das partidas =====
python pythoncode/collect_match_timeline.py --input D:/Data/MatchIds.csv --output D:/Data/MatchTimelineAllMinutes.csv

echo.
echo Pipeline de coleta finalizado.
pause