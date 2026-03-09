"""
Modern Native Windows Overlay for LoL Win Probability.

Radiant Arc HUD — a sleek overlay using PySide6 (Qt) with:
- Animated semicircular probability gauge
- Team-colored glowing left accent strip
- Card-style recommendations with emoji icons
- Smooth animated transitions

Run with pythonw.exe to avoid console window, or compile with --noconsole.
"""

import math
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

# Prevent console window flashes from subprocess calls (LightGBM/sklearn)
# when running as a frozen PyInstaller exe with --noconsole.
if sys.platform == 'win32':
    _CREATE_NO_WINDOW = 0x08000000
    _orig_popen_init = subprocess.Popen.__init__

    def _silent_popen_init(self, *args, **kwargs):
        kwargs.setdefault('creationflags', 0)
        kwargs['creationflags'] |= _CREATE_NO_WINDOW
        _orig_popen_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = _silent_popen_init

# PySide6 imports
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QGraphicsDropShadowEffect, QGraphicsOpacityEffect
)
from PySide6.QtCore import (
    Qt, QTimer, QPoint, QPropertyAnimation, QEasingCurve, QRectF,
    Property, QParallelAnimationGroup, Signal, QObject, QThread
)
from PySide6.QtGui import (
    QColor, QPainter, QBrush, QPen, QLinearGradient, QConicalGradient,
    QFont, QFontDatabase, QCursor, QPainterPath, QRegion, QRadialGradient
)

# Data processing
import requests
import urllib3
import joblib
import pandas as pd

# Disable SSL warnings (LoL client uses self-signed certificate)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# =============================================================================
# CONFIGURATION
# =============================================================================

WINDOW_WIDTH = 230
WINDOW_HEIGHT_COMPACT = 180
WINDOW_HEIGHT_EXPANDED = 365
CORNER_RADIUS = 20
ARC_WIDGET_HEIGHT = 120
POLL_INTERVAL_MS = 10000  # 10 seconds when game is active
RETRY_INTERVAL_MS = 2000   # 2 seconds when waiting for game
LOL_API_URL = "https://127.0.0.1:2999"

# Radiant Arc HUD color palette
class Colors:
    # Backgrounds — deeper purple-blue
    BG_PRIMARY = QColor(12, 10, 24, 245)       # Deep near-black
    BG_SECONDARY = QColor(22, 18, 42, 250)     # Slightly lighter for gradient top
    BG_CARD = QColor(30, 25, 55, 220)          # Card / recommendation bg
    BG_HOVER = QColor(40, 35, 65, 230)         # Hover state

    # Accents — richer team colors
    BLUE = QColor(66, 135, 255)                # Team blue (vibrant)
    BLUE_GLOW = QColor(66, 135, 255, 80)       # Blue glow
    RED = QColor(255, 75, 75)                  # Team red (vibrant)
    RED_GLOW = QColor(255, 75, 75, 80)         # Red glow
    ORANGE = QColor(245, 158, 11)              # Waiting state
    GRAY = QColor(90, 95, 110)                 # Neutral / loading
    PURPLE_ACCENT = QColor(140, 100, 255)      # Purple accent

    # Probability colors — vivid
    PROB_HIGH = QColor(52, 211, 153)           # >= 60% (emerald green)
    PROB_MID = QColor(251, 191, 36)            # ~50% (amber)
    PROB_LOW = QColor(251, 85, 85)             # <= 40% (warm red)

    # Arc gauge
    ARC_TRACK = QColor(35, 30, 60, 180)        # Dark unfilled arc track

    # Text
    TEXT_PRIMARY = QColor(255, 255, 255)
    TEXT_SECONDARY = QColor(170, 175, 195)
    TEXT_MUTED = QColor(100, 105, 125)


# Emoji icons for recommendation cards
SCENARIO_ICONS = {
    "Dragão": "\U0001F409",       # 🐉
    "Barão": "\U0001F451",        # 👑
    "Elder": "\U0001F525",        # 🔥
    "Arauto": "\U0001F6E1\uFE0F", # 🛡️
    "Voidgrubs": "\U0001F41B",    # 🐛
    "Torre": "\U0001F3F0",        # 🏰
    "Farm +10 CS": "\U00002694\uFE0F",  # ⚔️
    "Teamfight (2 kills)": "\U0001F4A5", # 💥
    "Inibidor": "\U0001F6A7",     # 🚧
}


# Model features in correct order (matches models/features.joblib)
MODEL_FEATURES = [
    'time',
    'p1_level', 'p1_spentGold', 'p1_totalCS',
    'p2_level', 'p2_spentGold', 'p2_totalCS',
    'p3_level', 'p3_spentGold', 'p3_totalCS',
    'p4_level', 'p4_spentGold', 'p4_totalCS',
    'p5_level', 'p5_spentGold', 'p5_totalCS',
    'p6_level', 'p6_spentGold', 'p6_totalCS',
    'p7_level', 'p7_spentGold', 'p7_totalCS',
    'p8_level', 'p8_spentGold', 'p8_totalCS',
    'p9_level', 'p9_spentGold', 'p9_totalCS',
    'p10_level', 'p10_spentGold', 'p10_totalCS',
    'spentGoldDiff', 'levelDiff', 'csDiff', 'killDiff',
    'blue_deaths', 'red_deaths', 'blue_assists', 'red_assists',
    'dragonDiff', 'elderDragonDiff', 'baronDiff', 'voidgrubDiff', 'heraldDiff',
    'towerDiff', 'blue_inhibitors', 'red_inhibitors',
    'eliteMonsterDiff', 'blue_kd_ratio', 'red_kd_ratio', 'csPerMinDiff',
]


# =============================================================================
# WHAT-IF SCENARIO ENGINE
# =============================================================================

@dataclass
class Scenario:
    name: str
    mutations: Dict[str, float]      # {feature: delta} applied with team sign
    abs_mutations: Dict[str, float]  # {feature: value} set absolutely (not sign-flipped)
    derived: List[str]               # which derived features to recompute
    min_time: float = 0.0
    max_time: float = float('inf')


@dataclass
class Recommendation:
    name: str
    delta_prob: float  # positive = good for the player's team


# Scenario definitions — mutations are from blue team perspective;
# delta sign is inverted automatically for red team players.
_SCENARIOS: List[Scenario] = [
    Scenario(
        name="Dragão",
        mutations={'dragonDiff': 1, 'eliteMonsterDiff': 1},
        abs_mutations={},
        derived=[],
        min_time=5.0,
    ),
    Scenario(
        name="Barão",
        mutations={'baronDiff': 1, 'eliteMonsterDiff': 1},
        abs_mutations={},
        derived=[],
        min_time=20.0,
    ),
    Scenario(
        name="Elder",
        mutations={'elderDragonDiff': 1, 'eliteMonsterDiff': 1},
        abs_mutations={},
        derived=[],
        min_time=35.0,
    ),
    Scenario(
        name="Arauto",
        mutations={'heraldDiff': 1, 'eliteMonsterDiff': 1},
        abs_mutations={},
        derived=[],
        min_time=5.0,
        max_time=20.0,
    ),
    Scenario(
        name="Voidgrubs",
        mutations={'voidgrubDiff': 1, 'eliteMonsterDiff': 1},
        abs_mutations={},
        derived=[],
        min_time=5.0,
        max_time=20.0,
    ),
    Scenario(
        name="Torre",
        mutations={'towerDiff': 1},
        abs_mutations={},
        derived=[],
    ),
    Scenario(
        name="Farm +10 CS",
        mutations={'csDiff': 10},
        abs_mutations={},
        derived=['csPerMinDiff'],
    ),
    Scenario(
        name="Teamfight (2 kills)",
        mutations={'killDiff': 2},
        abs_mutations={},
        derived=['blue_kd_ratio', 'red_kd_ratio'],
    ),
    Scenario(
        name="Inibidor",
        mutations={},
        abs_mutations={},
        derived=[],
        min_time=15.0,
    ),
]


class ScenarioEngine:
    """Simulates what-if scenarios and returns win-probability deltas."""

    def __init__(self, model):
        self.model = model

    def compute_recommendations(
        self,
        base_df: 'pd.DataFrame',
        time_minutes: float,
        player_team: str,
    ) -> List[Recommendation]:
        """Return top-3 recommendations sorted by positive impact on the player's team."""
        sign = 1 if player_team == 'blue' else -1

        valid = [s for s in _SCENARIOS if s.min_time <= time_minutes < s.max_time]

        if not valid:
            return []

        # Inhibitor scenario needs special abs handling based on team
        # Build perturbed DataFrames
        scenario_dfs = []
        scenario_names = []

        base_row = base_df.iloc[0].to_dict()

        for scenario in valid:
            row = base_row.copy()

            if scenario.name == "Inibidor":
                # Absolute: increment the player team's inhibitor count
                key = 'blue_inhibitors' if player_team == 'blue' else 'red_inhibitors'
                row[key] = row.get(key, 0) + 1
            else:
                # Apply signed mutations
                for feat, delta in scenario.mutations.items():
                    row[feat] = row.get(feat, 0) + sign * delta

            # Recompute derived features
            if 'csPerMinDiff' in scenario.derived and time_minutes > 0:
                row['csPerMinDiff'] = row['csDiff'] / time_minutes
            if 'blue_kd_ratio' in scenario.derived:
                row['blue_kd_ratio'] = row.get('killDiff', 0) + row.get('red_kills', 0)
                # Recompute properly: blue_kills = killDiff + red_kills
                # We don't store individual kills, so use the diff change
                # blue_kills_new = blue_kills_old + sign*2, blue_deaths unchanged
                # Approximate: adjust kd_ratio proportionally
                old_kill_diff = base_row.get('killDiff', 0)
                new_kill_diff = row.get('killDiff', 0)
                delta_kills = new_kill_diff - old_kill_diff
                # blue gains kills, red gains deaths when sign=1
                blue_deaths = row.get('blue_deaths', 1)
                red_deaths = row.get('red_deaths', 1)
                if sign == 1:
                    # blue got 2 kills -> red got 2 deaths
                    blue_kills_base = base_row.get('blue_kd_ratio', 1) * max(blue_deaths, 1)
                    red_deaths_new = red_deaths + 2
                    row['blue_kd_ratio'] = (blue_kills_base + 2) / max(blue_deaths, 1)
                    row['red_kd_ratio'] = base_row.get('red_kd_ratio', 1) * red_deaths / max(red_deaths_new, 1)
                else:
                    # red got 2 kills -> blue got 2 deaths
                    red_kills_base = base_row.get('red_kd_ratio', 1) * max(red_deaths, 1)
                    blue_deaths_new = blue_deaths + 2
                    row['red_kd_ratio'] = (red_kills_base + 2) / max(red_deaths, 1)
                    row['blue_kd_ratio'] = base_row.get('blue_kd_ratio', 1) * blue_deaths / max(blue_deaths_new, 1)

            scenario_dfs.append([row.get(f, 0) for f in MODEL_FEATURES])
            scenario_names.append(scenario.name)

        if not scenario_dfs:
            return []

        batch = pd.DataFrame(scenario_dfs, columns=MODEL_FEATURES)
        base_prob = float(self.model.predict_proba(base_df)[0][1])
        scenario_probs = self.model.predict_proba(batch)[:, 1]

        recommendations = []
        for name, prob in zip(scenario_names, scenario_probs):
            delta_blue = float(prob) - base_prob
            # From the player's perspective
            delta = delta_blue if player_team == 'blue' else -delta_blue
            if delta > 0.001:  # Only show meaningful improvements
                recommendations.append(Recommendation(name=name, delta_prob=delta))

        recommendations.sort(key=lambda r: r.delta_prob, reverse=True)
        return recommendations[:3]


# =============================================================================
# PATH UTILITIES
# =============================================================================

def get_base_path() -> Path:
    """Returns base path, works both in development and when packaged."""
    if getattr(sys, 'frozen', False):
        return Path(sys._MEIPASS)
    else:
        return Path(__file__).parent.parent.parent


def get_models_path() -> Path:
    """Returns path to models directory."""
    return get_base_path() / "models"


# =============================================================================
# FEATURE EXTRACTOR
# =============================================================================

class FeatureExtractor:
    """Extracts and transforms data from LoL Live Client API."""
    
    def __init__(self):
        self.base_url = LOL_API_URL
        self._player_teams: Dict[str, str] = {}
        self._item_prices: Dict[int, int] = {}
        self._prices_loaded = False
        threading.Thread(target=self._load_item_prices, daemon=True).start()

    def _load_item_prices(self):
        """Fetch item total prices from Data Dragon (runs in background thread)."""
        try:
            versions = requests.get(
                "https://ddragon.leagueoflegends.com/api/versions.json", timeout=10
            ).json()
            latest = versions[0]
            items_data = requests.get(
                f"https://ddragon.leagueoflegends.com/cdn/{latest}/data/en_US/item.json",
                timeout=30,
            ).json()
            prices = {}
            for id_str, info in items_data.get("data", {}).items():
                try:
                    prices[int(id_str)] = info.get("gold", {}).get("total", 0)
                except (ValueError, TypeError):
                    continue
            self._item_prices = prices
            self._prices_loaded = True
        except Exception:
            pass

    def _get_player_spent_gold(self, items: List[Dict[str, Any]]) -> int:
        """Estimate gold spent from a player's current item list.

        Uses Data Dragon total prices (same metric as training data).
        For stackable consumables (potions) the count field is used.
        Returns 0 until Data Dragon prices have loaded.
        """
        if not self._prices_loaded:
            return 0
        total = 0
        for item in items:
            item_id = item.get('itemID', 0)
            if item_id <= 0:
                continue
            count = max(item.get('count', 1), 1)
            total += self._item_prices.get(item_id, 0) * count
        return total
    
    def check_game_active(self) -> bool:
        """Check if a game is currently active."""
        try:
            response = requests.get(
                f"{self.base_url}/liveclientdata/gamestats",
                verify=False,
                timeout=2
            )
            return response.status_code == 200
        except requests.exceptions.RequestException:
            return False
    
    def get_live_data(self) -> Optional[Dict[str, Any]]:
        """Fetch all game data from the Live Client API."""
        try:
            response = requests.get(
                f"{self.base_url}/liveclientdata/allgamedata",
                verify=False,
                timeout=5
            )
            if response.status_code == 200:
                return response.json()
            return None
        except requests.exceptions.RequestException:
            return None
    
    def _build_player_team_map(self, players: List[Dict[str, Any]]) -> Dict[str, str]:
        """Build a map of player name -> team."""
        player_teams = {}
        for player in players:
            name = player.get('riotIdGameName', '')
            team = player.get('team', '')
            if name:
                player_teams[name] = 'blue' if team == 'ORDER' else 'red'
        return player_teams
    
    def get_player_team(self, data: Dict[str, Any]) -> Optional[str]:
        """Identify which team the player is on."""
        if 'activePlayer' not in data or 'allPlayers' not in data:
            return None
        
        active_player_name = data['activePlayer'].get('riotIdGameName', '')
        
        for player in data['allPlayers']:
            player_name = player.get('riotIdGameName', '')
            if player_name == active_player_name:
                team = player.get('team', '')
                return 'blue' if team == 'ORDER' else 'red'
        
        return None
    
    def extract_features(self, data: Dict[str, Any]) -> Optional[Tuple[pd.DataFrame, Dict[str, Any]]]:
        """Extract features from Live Client API data."""
        if not data:
            return None
        
        try:
            game_time = data.get('gameData', {}).get('gameTime', 0)
            time_minutes = game_time / 60.0
            player_team = self.get_player_team(data)
            
            blue_players = []
            red_players = []
            
            for player in data.get('allPlayers', []):
                team = player.get('team', '')
                if team == 'ORDER':
                    blue_players.append(player)
                else:
                    red_players.append(player)
            
            position_order = {'TOP': 0, 'JUNGLE': 1, 'MIDDLE': 2, 'BOTTOM': 3, 'UTILITY': 4, '': 5}
            blue_players.sort(key=lambda p: position_order.get(p.get('position', ''), 5))
            red_players.sort(key=lambda p: position_order.get(p.get('position', ''), 5))
            
            player_stats = {}
            
            for i, player in enumerate(blue_players[:5], 1):
                scores = player.get('scores', {})
                player_stats[f'p{i}_level'] = player.get('level', 1)
                player_stats[f'p{i}_totalCS'] = scores.get('creepScore', 0)
                player_stats[f'p{i}_kills'] = scores.get('kills', 0)
                player_stats[f'p{i}_deaths'] = scores.get('deaths', 0)
                player_stats[f'p{i}_assists'] = scores.get('assists', 0)
                player_stats[f'p{i}_spentGold'] = self._get_player_spent_gold(player.get('items', []))

            for i, player in enumerate(red_players[:5], 6):
                scores = player.get('scores', {})
                player_stats[f'p{i}_level'] = player.get('level', 1)
                player_stats[f'p{i}_totalCS'] = scores.get('creepScore', 0)
                player_stats[f'p{i}_kills'] = scores.get('kills', 0)
                player_stats[f'p{i}_deaths'] = scores.get('deaths', 0)
                player_stats[f'p{i}_assists'] = scores.get('assists', 0)
                player_stats[f'p{i}_spentGold'] = self._get_player_spent_gold(player.get('items', []))
            
            blue_kills = sum(player_stats.get(f'p{i}_kills', 0) for i in range(1, 6))
            red_kills = sum(player_stats.get(f'p{i}_kills', 0) for i in range(6, 11))
            blue_deaths = sum(player_stats.get(f'p{i}_deaths', 0) for i in range(1, 6))
            red_deaths = sum(player_stats.get(f'p{i}_deaths', 0) for i in range(6, 11))
            blue_assists = sum(player_stats.get(f'p{i}_assists', 0) for i in range(1, 6))
            red_assists = sum(player_stats.get(f'p{i}_assists', 0) for i in range(6, 11))
            blue_total_cs = sum(player_stats.get(f'p{i}_totalCS', 0) for i in range(1, 6))
            red_total_cs = sum(player_stats.get(f'p{i}_totalCS', 0) for i in range(6, 11))
            blue_avg_level = sum(player_stats.get(f'p{i}_level', 1) for i in range(1, 6)) / 5
            red_avg_level = sum(player_stats.get(f'p{i}_level', 1) for i in range(6, 11)) / 5
            
            blue_kd_ratio = blue_kills / max(blue_deaths, 1)
            red_kd_ratio = red_kills / max(red_deaths, 1)
            
            if time_minutes > 0:
                blue_cs_per_min = blue_total_cs / time_minutes
                red_cs_per_min = red_total_cs / time_minutes
            else:
                blue_cs_per_min = 0
                red_cs_per_min = 0
            
            self._player_teams = self._build_player_team_map(data.get('allPlayers', []))
            events = data.get('events', {}).get('Events', [])
            objectives = self._count_objectives(events)
            
            features = {
                'time': time_minutes,
                'levelDiff': blue_avg_level - red_avg_level,
                'killDiff': blue_kills - red_kills,
                'csDiff': blue_total_cs - red_total_cs,
                'csPerMinDiff': blue_cs_per_min - red_cs_per_min,
                'dragonDiff': objectives['blue_dragons'] - objectives['red_dragons'],
                'elderDragonDiff': objectives['blue_elders'] - objectives['red_elders'],
                'baronDiff': objectives['blue_barons'] - objectives['red_barons'],
                'heraldDiff': objectives['blue_heralds'] - objectives['red_heralds'],
                'voidgrubDiff': objectives['blue_voidgrubs'] - objectives['red_voidgrubs'],
                'towerDiff': objectives['blue_towers'] - objectives['red_towers'],
                'blue_inhibitors': objectives['blue_inhibitors'],
                'red_inhibitors': objectives['red_inhibitors'],
                'eliteMonsterDiff': (
                    objectives['blue_dragons'] + objectives['blue_barons'] +
                    objectives['blue_heralds'] + objectives['blue_voidgrubs'] +
                    objectives['blue_elders']
                ) - (
                    objectives['red_dragons'] + objectives['red_barons'] +
                    objectives['red_heralds'] + objectives['red_voidgrubs'] +
                    objectives['red_elders']
                ),
                'blue_kd_ratio': blue_kd_ratio,
                'red_kd_ratio': red_kd_ratio,
                'blue_assists': blue_assists,
                'red_assists': red_assists,
                'blue_deaths': blue_deaths,
                'red_deaths': red_deaths,
                **{f'p{i}_totalCS': player_stats.get(f'p{i}_totalCS', 0) for i in range(1, 11)},
                **{f'p{i}_level': player_stats.get(f'p{i}_level', 1) for i in range(1, 11)},
                **{f'p{i}_spentGold': player_stats.get(f'p{i}_spentGold', 0) for i in range(1, 11)},
                'spentGoldDiff': (
                    sum(player_stats.get(f'p{i}_spentGold', 0) for i in range(1, 6)) -
                    sum(player_stats.get(f'p{i}_spentGold', 0) for i in range(6, 11))
                ),
            }
            
            df = pd.DataFrame([{feat: features.get(feat, 0) for feat in MODEL_FEATURES}])
            
            metadata = {
                'game_time': game_time,
                'time_minutes': time_minutes,
                'player_team': player_team,
                'blue_kills': blue_kills,
                'red_kills': red_kills,
            }
            
            return df, metadata
            
        except Exception:
            return None
    
    def _count_objectives(self, events: List[Dict[str, Any]]) -> Dict[str, int]:
        """Count objectives per team from events."""
        objectives = {
            'blue_dragons': 0, 'red_dragons': 0,
            'blue_elders': 0, 'red_elders': 0,
            'blue_barons': 0, 'red_barons': 0,
            'blue_heralds': 0, 'red_heralds': 0,
            'blue_voidgrubs': 0, 'red_voidgrubs': 0,
            'blue_towers': 0, 'red_towers': 0,
            'blue_inhibitors': 0, 'red_inhibitors': 0,
        }

        for event in events:
            event_name = event.get('EventName', '')

            if event_name == 'DragonKill':
                dragon_type = event.get('DragonType', '')
                is_blue = self._is_blue_team_kill(event)
                if dragon_type == 'Elder':
                    objectives['blue_elders' if is_blue else 'red_elders'] += 1
                else:
                    objectives['blue_dragons' if is_blue else 'red_dragons'] += 1

            elif event_name == 'BaronKill':
                key = 'blue_barons' if self._is_blue_team_kill(event) else 'red_barons'
                objectives[key] += 1

            elif event_name == 'HeraldKill':
                key = 'blue_heralds' if self._is_blue_team_kill(event) else 'red_heralds'
                objectives[key] += 1

            elif event_name == 'HordeKill':
                key = 'blue_voidgrubs' if self._is_blue_team_kill(event) else 'red_voidgrubs'
                objectives[key] += 1

            elif event_name == 'TurretKilled':
                # Killer destroys enemy's turret, so the killer's team gains the tower
                key = 'blue_towers' if self._is_blue_team_kill(event) else 'red_towers'
                objectives[key] += 1

            elif event_name == 'InhibKilled':
                key = 'blue_inhibitors' if self._is_blue_team_kill(event) else 'red_inhibitors'
                objectives[key] += 1

        return objectives
    
    def _is_blue_team_kill(self, event: Dict[str, Any]) -> bool:
        """Determine if the kill was by blue team."""
        if 'Team' in event:
            return event['Team'] == 'ORDER'
        
        killer = event.get('KillerName', '')
        if killer and killer in self._player_teams:
            return self._player_teams[killer] == 'blue'
        
        assisters = event.get('Assisters', [])
        for assister in assisters:
            if assister in self._player_teams:
                return self._player_teams[assister] == 'blue'
        
        return True


# =============================================================================
# MODEL PREDICTOR
# =============================================================================

class Predictor:
    """Handles model loading and prediction."""

    def __init__(self):
        self.model = None
        self.feature_names = None
        self.extractor = FeatureExtractor()
        self.scenario_engine: Optional[ScenarioEngine] = None
        self._load_model()

    def _load_model(self):
        """Load the trained model and feature list."""
        models_path = get_models_path()
        model_path = models_path / "lol_win_predictor_lgbm_isotonic.joblib"
        features_path = models_path / "features.joblib"

        if model_path.exists():
            self.model = joblib.load(model_path)
            self.scenario_engine = ScenarioEngine(self.model)

        if features_path.exists():
            self.feature_names = joblib.load(features_path)
        else:
            self.feature_names = MODEL_FEATURES

    def get_prediction(self) -> Dict[str, Any]:
        """Get current win probability prediction."""
        if self.model is None:
            return {'status': 'no_model', 'message': 'Model not loaded'}

        if not self.extractor.check_game_active():
            return {'status': 'no_game', 'message': 'Waiting for game...'}

        live_data = self.extractor.get_live_data()
        if not live_data:
            # API responded to gamestats but not allgamedata - likely game ended
            return {'status': 'no_game', 'message': 'Waiting for game...'}

        # Check for GameEnd event before anything else
        events = live_data.get('events', {}).get('Events', [])
        if any(e.get('EventName') == 'GameEnd' for e in events):
            return {'status': 'game_over'}

        result = self.extractor.extract_features(live_data)
        if not result:
            return {'status': 'error', 'message': 'Failed to extract features'}

        df, metadata = result
        game_time = metadata['game_time']
        minutes = int(game_time // 60)
        seconds = int(game_time % 60)
        time_formatted = f"{minutes}:{seconds:02d}"

        # Too early — not enough information for a reliable prediction
        if metadata['time_minutes'] < 3:
            return {
                'status': 'early_game',
                'time_formatted': time_formatted,
                'player_team': metadata['player_team'],
            }

        try:
            if hasattr(self.model, 'predict_proba'):
                proba = self.model.predict_proba(df)[0]
                blue_win_prob = float(proba[1])
            else:
                pred = self.model.predict(df)[0]
                blue_win_prob = float(pred)

            player_team = metadata['player_team']

            recommendations: List[Recommendation] = []
            if self.scenario_engine and player_team:
                try:
                    recommendations = self.scenario_engine.compute_recommendations(
                        df, metadata['time_minutes'], player_team
                    )
                except Exception:
                    pass  # Recommendations are best-effort; never crash the main prediction

            return {
                'status': 'ok',
                'probability': blue_win_prob,
                'player_team': player_team,
                'game_time': game_time,
                'time_formatted': time_formatted,
                'recommendations': recommendations,
            }

        except Exception as e:
            return {'status': 'error', 'message': str(e)}


# =============================================================================
# PREDICTION WORKER (Thread-safe using QThread)
# =============================================================================

class PredictionThread(QThread):
    """Thread that fetches predictions safely."""
    prediction_ready = Signal(dict)
    
    def __init__(self, predictor: Predictor, parent=None):
        super().__init__(parent)
        self.predictor = predictor
    
    def run(self):
        """Fetch prediction and emit signal."""
        data = self.predictor.get_prediction()
        self.prediction_ready.emit(data)


# =============================================================================
# ARC GAUGE WIDGET
# =============================================================================

class ArcGaugeWidget(QWidget):
    """Animated semicircular probability gauge."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(WINDOW_WIDTH - 32, ARC_WIDGET_HEIGHT)

        self._display_prob = 0.0      # animated value 0-1
        self._arc_color = Colors.GRAY
        self._prob_text = "--"
        self._time_text = ""

        # Animation for smooth arc transitions
        self._anim = QPropertyAnimation(self, b"displayProb")
        self._anim.setDuration(400)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)

    # --- Qt property for animation ---
    def _get_display_prob(self):
        return self._display_prob

    def _set_display_prob(self, val):
        self._display_prob = val
        self.update()

    displayProb = Property(float, _get_display_prob, _set_display_prob)

    def setProbability(self, prob: float, color: QColor, text: str):
        """Animate arc to new probability value."""
        self._arc_color = color
        self._prob_text = text
        self._anim.stop()
        self._anim.setStartValue(self._display_prob)
        self._anim.setEndValue(prob)
        self._anim.start()

    def setProbText(self, text: str):
        self._prob_text = text
        self.update()

    def setTimeText(self, text: str):
        self._time_text = text
        self.update()

    def setArcColor(self, color: QColor):
        self._arc_color = color
        self.update()

    def resetArc(self):
        """Reset to idle state (no animation)."""
        self._anim.stop()
        self._display_prob = 0.0
        self._arc_color = Colors.GRAY
        self._prob_text = "--"
        self._time_text = ""
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        w = self.width()
        h = self.height()
        arc_thickness = 8
        margin = 12
        arc_diameter = min(w - 2 * margin, (h - 10) * 2)
        arc_rect = QRectF(
            (w - arc_diameter) / 2,
            margin,
            arc_diameter,
            arc_diameter
        )

        start_angle = 180 * 16   # Qt uses 1/16th degrees; 180° = left
        full_span = -180 * 16    # sweep clockwise (negative)

        # Layer 1: dark track
        pen_track = QPen(Colors.ARC_TRACK, arc_thickness, Qt.SolidLine, Qt.RoundCap)
        painter.setPen(pen_track)
        painter.drawArc(arc_rect, start_angle, full_span)

        # Layer 2: glow (wider, semi-transparent)
        if self._display_prob > 0.005:
            span = int(full_span * self._display_prob)
            glow_color = QColor(self._arc_color)
            glow_color.setAlpha(50)
            pen_glow = QPen(glow_color, arc_thickness + 6, Qt.SolidLine, Qt.RoundCap)
            painter.setPen(pen_glow)
            painter.drawArc(arc_rect, start_angle, span)

        # Layer 3: solid fill
        if self._display_prob > 0.005:
            span = int(full_span * self._display_prob)
            pen_fill = QPen(self._arc_color, arc_thickness, Qt.SolidLine, Qt.RoundCap)
            painter.setPen(pen_fill)
            painter.drawArc(arc_rect, start_angle, span)

        # Probability text (centered in arc)
        painter.setPen(Qt.NoPen)
        text_color = QColor(self._arc_color) if self._prob_text != "--" else Colors.TEXT_SECONDARY
        painter.setPen(text_color)
        prob_font = QFont(painter.font())
        prob_font.setPixelSize(32)
        prob_font.setBold(True)
        painter.setFont(prob_font)
        text_y = margin + arc_diameter / 2 - 4
        painter.drawText(QRectF(0, text_y - 22, w, 30), Qt.AlignCenter, self._prob_text)

        # Time text (below probability)
        if self._time_text:
            painter.setPen(Colors.TEXT_SECONDARY)
            time_font = QFont(painter.font())
            time_font.setPixelSize(12)
            time_font.setBold(False)
            painter.setFont(time_font)
            painter.drawText(QRectF(0, text_y + 8, w, 18), Qt.AlignCenter, self._time_text)

        painter.end()


# =============================================================================
# MODERN OVERLAY WIDGET
# =============================================================================

class ModernOverlay(QWidget):
    """Radiant Arc HUD overlay widget."""

    def __init__(self):
        super().__init__()

        self.predictor = Predictor()
        self._prediction_thread = None

        # State
        self.current_team = None
        self.is_dragging = False
        self.drag_position = QPoint()
        self._hover = False
        self._expanded = False
        self._current_recommendations: List[Recommendation] = []
        self._game_was_active = False
        self._interactive_widgets = set()

        # Team strip state
        self._team_strip_color = Colors.GRAY
        self._team_strip_opacity = 0.8

        self._setup_window()
        self._create_ui()
        self._setup_animations()
        self._start_polling()

    # --- Animatable fixedHeight property ---
    def _get_fixed_height(self):
        return self.height()

    def _set_fixed_height(self, h):
        self.setFixedSize(WINDOW_WIDTH, int(h))

    fixedHeight = Property(int, _get_fixed_height, _set_fixed_height)

    def _setup_window(self):
        """Configure window properties."""
        self.setWindowFlags(
            Qt.FramelessWindowHint |
            Qt.WindowStaysOnTopHint |
            Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(WINDOW_WIDTH, WINDOW_HEIGHT_COMPACT)

        # Position in top-right corner
        screen = QApplication.primaryScreen().geometry()
        self.move(screen.width() - WINDOW_WIDTH - 30, 30)

        # Drop shadow — blue-tinted
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(30)
        shadow.setColor(QColor(20, 15, 60, 90))
        shadow.setOffset(0, 5)
        self.setGraphicsEffect(shadow)

        self.setCursor(QCursor(Qt.OpenHandCursor))

    def _create_ui(self):
        """Create the Radiant Arc HUD layout."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 10, 16, 12)
        layout.setSpacing(2)

        # Arc gauge widget (probability + time)
        self.arc_gauge = ArcGaugeWidget(self)
        layout.addWidget(self.arc_gauge, alignment=Qt.AlignCenter)

        # Status message
        self.status_label = QLabel("Aguardando partida...")
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setStyleSheet(f"""
            font-size: 11px;
            color: {Colors.TEXT_MUTED.name()};
            padding-top: 0px;
        """)
        layout.addWidget(self.status_label)

        # Expand/collapse pill button (centered)
        btn_container = QHBoxLayout()
        btn_container.setSpacing(0)
        btn_container.addStretch()
        self.expand_btn = QPushButton("\u25BE Dicas")
        self.expand_btn.setStyleSheet(f"""
            QPushButton {{
                color: {Colors.TEXT_SECONDARY.name()};
                font-size: 10px;
                font-weight: 600;
                padding: 3px 14px;
                border-radius: 9px;
                background: rgba(255,255,255,8);
                border: none;
                outline: none;
            }}
            QPushButton:hover {{
                color: {Colors.TEXT_PRIMARY.name()};
                background: rgba(255,255,255,18);
            }}
        """)
        self.expand_btn.setCursor(QCursor(Qt.PointingHandCursor))
        self.expand_btn.clicked.connect(self._toggle_expand)
        self.expand_btn.hide()
        self._interactive_widgets.add(self.expand_btn)
        btn_container.addWidget(self.expand_btn)
        btn_container.addStretch()
        layout.addLayout(btn_container)

        # --- Recommendations panel (hidden by default) ---
        self.rec_panel = QWidget(self)
        rec_layout = QVBoxLayout(self.rec_panel)
        rec_layout.setContentsMargins(0, 6, 0, 0)
        rec_layout.setSpacing(5)

        # Separator
        separator = QLabel()
        separator.setFixedHeight(1)
        separator.setStyleSheet("background: rgba(255,255,255,12); margin: 0 4px;")
        rec_layout.addWidget(separator)

        # Header
        rec_header = QLabel("AÇÕES RECOMENDADAS")
        rec_header.setAlignment(Qt.AlignCenter)
        rec_header.setStyleSheet(f"""
            font-size: 9px;
            color: {Colors.TEXT_MUTED.name()};
            font-weight: 700;
            letter-spacing: 1.5px;
            padding-bottom: 2px;
            padding-top: 2px;
        """)
        rec_layout.addWidget(rec_header)

        # 3 recommendation cards
        self._rec_cards: List[QWidget] = []
        self._rec_icon_labels: List[QLabel] = []
        self._rec_name_labels: List[QLabel] = []
        self._rec_delta_labels: List[QLabel] = []

        for _ in range(3):
            card = QWidget()
            card.setStyleSheet(f"""
                QWidget {{
                    background: rgba(255,255,255,6);
                    border-radius: 8px;
                }}
            """)
            card_layout = QHBoxLayout(card)
            card_layout.setContentsMargins(10, 6, 10, 6)
            card_layout.setSpacing(8)

            icon_lbl = QLabel("")
            icon_lbl.setFixedWidth(20)
            icon_lbl.setStyleSheet("font-size: 14px; background: transparent;")

            name_lbl = QLabel("")
            name_lbl.setStyleSheet(f"""
                font-size: 11px;
                color: {Colors.TEXT_SECONDARY.name()};
                background: transparent;
            """)

            delta_lbl = QLabel("")
            delta_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            delta_lbl.setStyleSheet(f"""
                font-size: 10px;
                font-weight: bold;
                color: {Colors.PROB_HIGH.name()};
                background: rgba(52,211,153,25);
                border-radius: 7px;
                padding: 2px 8px;
            """)

            card_layout.addWidget(icon_lbl)
            card_layout.addWidget(name_lbl, stretch=1)
            card_layout.addWidget(delta_lbl)
            rec_layout.addWidget(card)

            self._rec_cards.append(card)
            self._rec_icon_labels.append(icon_lbl)
            self._rec_name_labels.append(name_lbl)
            self._rec_delta_labels.append(delta_lbl)
            card.hide()

        self.rec_panel.hide()
        layout.addWidget(self.rec_panel)

        # Close button — circular, absolute positioned top-right
        self.close_btn = QLabel("\u2715", self)
        self.close_btn.setFixedSize(18, 18)
        self.close_btn.setAlignment(Qt.AlignCenter)
        self.close_btn.setStyleSheet("""
            QLabel {
                color: #6b7280;
                font-size: 10px;
                font-weight: bold;
                background: rgba(255,255,255,8);
                border-radius: 9px;
            }
            QLabel:hover {
                color: #f87171;
                background: rgba(248, 113, 113, 0.15);
            }
        """)
        self.close_btn.setCursor(QCursor(Qt.PointingHandCursor))
        self.close_btn.mousePressEvent = lambda e: self.close()
        self.close_btn.move(WINDOW_WIDTH - 26, 8)
        self.close_btn.hide()
        self._interactive_widgets.add(self.close_btn)
        self._interactive_widgets.add(self.expand_btn)

    def _setup_animations(self):
        """Set up height animation and team strip pulse timer."""
        # Height animation for expand/collapse
        self._height_anim = QPropertyAnimation(self, b"fixedHeight")
        self._height_anim.setDuration(200)
        self._height_anim.setEasingCurve(QEasingCurve.OutCubic)

        # Pulse timer for team strip (20 FPS)
        self._pulse_timer = QTimer(self)
        self._pulse_timer.setInterval(50)
        self._pulse_timer.timeout.connect(self._pulse_tick)
        self._pulse_phase = 0.0

    def _pulse_tick(self):
        """Oscillate team strip opacity between 0.8 and 1.0."""
        self._pulse_phase += 0.05 * math.pi
        self._team_strip_opacity = 0.9 + 0.1 * math.sin(self._pulse_phase)
        self.update()
    
    def paintEvent(self, event):
        """Custom paint for Radiant Arc HUD."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        w = self.width()
        h = self.height()

        # Rounded rectangle path
        path = QPainterPath()
        path.addRoundedRect(0, 0, w, h, CORNER_RADIUS, CORNER_RADIUS)

        # Background gradient (BG_SECONDARY top → BG_PRIMARY bottom)
        gradient = QLinearGradient(0, 0, 0, h)
        gradient.setColorAt(0, Colors.BG_SECONDARY)
        gradient.setColorAt(1, Colors.BG_PRIMARY)
        painter.fillPath(path, QBrush(gradient))

        # Team-colored left strip (3px wide, clipped to rounded rect)
        strip_path = QPainterPath()
        strip_path.addRect(0, 0, 3, h)
        clipped_strip = strip_path.intersected(path)
        strip_color = QColor(self._team_strip_color)
        strip_color.setAlphaF(self._team_strip_opacity)
        painter.fillPath(clipped_strip, QBrush(strip_color))

        # Team color bleed — subtle horizontal gradient from left edge
        bleed_color = QColor(self._team_strip_color)
        bleed_color.setAlpha(18)
        bleed_grad = QLinearGradient(0, 0, 60, 0)
        bleed_grad.setColorAt(0, bleed_color)
        bleed_grad.setColorAt(1, QColor(0, 0, 0, 0))
        painter.fillPath(path, QBrush(bleed_grad))

        # Inner glow at top edge (40px)
        glow_gradient = QLinearGradient(0, 0, 0, 40)
        glow_gradient.setColorAt(0, QColor(255, 255, 255, 10))
        glow_gradient.setColorAt(1, QColor(255, 255, 255, 0))
        painter.fillPath(path, QBrush(glow_gradient))

        # Subtle outer border
        painter.setPen(QPen(QColor(255, 255, 255, 8), 1))
        painter.drawPath(path)

        painter.end()
    
    def _get_probability_color(self, prob: float) -> QColor:
        """Get color with smooth linear interpolation in the 40-60% range."""
        if prob >= 0.6:
            return Colors.PROB_HIGH
        elif prob <= 0.4:
            return Colors.PROB_LOW
        else:
            # Smooth interpolation: 0.4→PROB_LOW, 0.5→PROB_MID, 0.6→PROB_HIGH
            if prob < 0.5:
                t = (prob - 0.4) / 0.1  # 0..1
                c1, c2 = Colors.PROB_LOW, Colors.PROB_MID
            else:
                t = (prob - 0.5) / 0.1  # 0..1
                c1, c2 = Colors.PROB_MID, Colors.PROB_HIGH
            r = int(c1.red()   + (c2.red()   - c1.red())   * t)
            g = int(c1.green() + (c2.green() - c1.green()) * t)
            b = int(c1.blue()  + (c2.blue()  - c1.blue())  * t)
            return QColor(r, g, b)
    
    def _update_team_indicator(self, team: str):
        """Update team strip color and pulse animation."""
        if team == 'blue':
            self._team_strip_color = Colors.BLUE
            self._pulse_phase = 0.0
            self._pulse_timer.start()
        elif team == 'red':
            self._team_strip_color = Colors.RED
            self._pulse_phase = 0.0
            self._pulse_timer.start()
        else:
            self._team_strip_color = Colors.GRAY
            self._team_strip_opacity = 0.8
            self._pulse_timer.stop()

        self.update()
    
    def _toggle_expand(self):
        """Toggle the recommendations panel with animated height transition."""
        self._expanded = not self._expanded
        self.expand_btn.setText("\u25B4 Dicas" if self._expanded else "\u25BE Dicas")

        if self._expanded:
            self.rec_panel.show()
            self._refresh_rec_rows()
            target = WINDOW_HEIGHT_EXPANDED
        else:
            self.rec_panel.hide()
            target = WINDOW_HEIGHT_COMPACT

        self._height_anim.stop()
        self._height_anim.setStartValue(self.height())
        self._height_anim.setEndValue(target)
        self._height_anim.start()

    def _refresh_rec_rows(self):
        """Populate recommendation cards from cached data."""
        recs = self._current_recommendations
        for i in range(3):
            if i < len(recs):
                rec = recs[i]
                name = rec.get('name', '') if isinstance(rec, dict) else getattr(rec, 'name', '')
                delta = rec.get('delta_prob', 0.0) if isinstance(rec, dict) else getattr(rec, 'delta_prob', 0.0)
                icon = SCENARIO_ICONS.get(str(name), "\u2728")
                self._rec_icon_labels[i].setText(icon)
                self._rec_name_labels[i].setText(str(name))
                self._rec_delta_labels[i].setText(f"+{float(delta) * 100:.1f}%")
                self._rec_cards[i].show()
            else:
                self._rec_cards[i].hide()

    def _on_prediction(self, data: Dict[str, Any]):
        """Handle prediction data update."""
        status = data.get('status', 'error')

        if status == 'ok':
            self._game_was_active = True
            blue_prob = data['probability']
            player_team = data.get('player_team', 'blue')

            # Adjust probability for player's team
            display_prob = 1.0 - blue_prob if player_team == 'red' else blue_prob
            prob_percent = int(display_prob * 100)

            # Update arc gauge
            prob_color = self._get_probability_color(display_prob)
            self.arc_gauge.setProbability(display_prob, prob_color, f"{prob_percent}%")
            self.arc_gauge.setTimeText(data.get('time_formatted', ''))

            # Hide status label
            self.status_label.hide()

            # Update recommendations cache
            self._current_recommendations = data.get('recommendations', [])
            has_recs = bool(self._current_recommendations)
            if has_recs:
                self.expand_btn.show()
            else:
                self.expand_btn.hide()
                if self._expanded:
                    self._expanded = False
                    self.rec_panel.hide()
                    self._height_anim.stop()
                    self._height_anim.setStartValue(self.height())
                    self._height_anim.setEndValue(WINDOW_HEIGHT_COMPACT)
                    self._height_anim.start()

            if self._expanded:
                self._refresh_rec_rows()

            # Update team indicator
            if player_team != self.current_team:
                self.current_team = player_team
                self._update_team_indicator(player_team)

        elif status == 'game_over':
            self._game_was_active = False
            self.arc_gauge.resetArc()
            self.arc_gauge.setProbText("--")
            self.arc_gauge.setTimeText("")
            self.status_label.setText("Partida encerrada")
            self.status_label.show()
            self._update_team_indicator('none')
            self.current_team = None
            self._current_recommendations = []
            self.expand_btn.hide()
            if self._expanded:
                self._expanded = False
                self.rec_panel.hide()
                self._height_anim.stop()
                self._height_anim.setStartValue(self.height())
                self._height_anim.setEndValue(WINDOW_HEIGHT_COMPACT)
                self._height_anim.start()
            self.update()

        elif status == 'early_game':
            player_team = data.get('player_team')
            self.arc_gauge.resetArc()
            self.arc_gauge.setProbText("--")
            self.arc_gauge.setTimeText(data.get('time_formatted', ''))
            self.status_label.setText("Aguardando 3min...")
            self.status_label.show()
            if player_team != self.current_team:
                self.current_team = player_team
                self._update_team_indicator(player_team or 'none')
            self._current_recommendations = []
            self.expand_btn.hide()

        elif status == 'no_game':
            self.arc_gauge.resetArc()
            self.arc_gauge.setProbText("--")
            self.arc_gauge.setTimeText("")
            if self._game_was_active:
                self.status_label.setText("Partida encerrada")
                self._game_was_active = False
            else:
                self.status_label.setText("Aguardando partida...")
            self.status_label.show()
            self._update_team_indicator('none')
            self.current_team = None
            self._current_recommendations = []
            self.expand_btn.hide()
            if self._expanded:
                self._expanded = False
                self.rec_panel.hide()
                self._height_anim.stop()
                self._height_anim.setStartValue(self.height())
                self._height_anim.setEndValue(WINDOW_HEIGHT_COMPACT)
                self._height_anim.start()
            self.update()

        elif status == 'no_model':
            self.arc_gauge.resetArc()
            self.arc_gauge.setProbText("!")
            self.arc_gauge.setArcColor(Colors.PROB_LOW)
            self.arc_gauge.setTimeText("")
            self.status_label.setText("Model not found")
            self.status_label.show()
            self._team_strip_color = Colors.PROB_LOW
            self.update()

        else:
            self.arc_gauge.resetArc()
            self.arc_gauge.setProbText("?")
            self.arc_gauge.setArcColor(Colors.PROB_LOW)
            self.arc_gauge.setTimeText("")
            msg = data.get('message', 'Error')[:25]
            self.status_label.setText(msg)
            self.status_label.show()
            self._team_strip_color = Colors.PROB_LOW
            self.update()
    
    def _fetch_prediction(self):
        """Fetch prediction in background QThread."""
        # Clean up old thread if it exists
        if self._prediction_thread is not None:
            if self._prediction_thread.isRunning():
                return  # Don't interrupt running thread
            # Disconnect old signal to avoid multiple connections
            try:
                self._prediction_thread.prediction_ready.disconnect(self._on_prediction)
            except RuntimeError:
                pass  # Already disconnected
            self._prediction_thread.deleteLater()
            self._prediction_thread = None
        
        self._prediction_thread = PredictionThread(self.predictor, self)
        self._prediction_thread.prediction_ready.connect(self._on_prediction, Qt.QueuedConnection)
        self._prediction_thread.start()
    
    def _start_polling(self):
        """Start the prediction polling timer."""
        self._fetch_prediction()
        
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._on_timer)
        self.timer.start(RETRY_INTERVAL_MS)
    
    def _on_timer(self):
        """Handle timer tick."""
        self._fetch_prediction()
        
        # Adjust interval based on game state
        if self.current_team is not None:
            self.timer.setInterval(POLL_INTERVAL_MS)
        else:
            self.timer.setInterval(RETRY_INTERVAL_MS)
    
    # -------------------------------------------------------------------------
    # Mouse events for dragging
    # -------------------------------------------------------------------------
    
    def enterEvent(self, event):
        """Show close button on hover."""
        self._hover = True
        self.close_btn.show()
        super().enterEvent(event)

    def leaveEvent(self, event):
        """Hide close button when not hovering."""
        self._hover = False
        self.close_btn.hide()
        super().leaveEvent(event)
    
    def mousePressEvent(self, event):
        """Start dragging."""
        if event.button() == Qt.LeftButton:
            target = self.childAt(event.position().toPoint())
            if target in self._interactive_widgets:
                event.ignore()
                return
            self.is_dragging = True
            self.drag_position = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self.setCursor(QCursor(Qt.ClosedHandCursor))
            event.accept()
    
    def mouseMoveEvent(self, event):
        """Handle dragging."""
        if self.is_dragging:
            self.move(event.globalPosition().toPoint() - self.drag_position)
            event.accept()
    
    def mouseReleaseEvent(self, event):
        """End dragging."""
        if event.button() == Qt.LeftButton:
            self.is_dragging = False
            self.setCursor(QCursor(Qt.OpenHandCursor))
            event.accept()
    
    def keyPressEvent(self, event):
        """Handle keyboard shortcuts."""
        if event.key() == Qt.Key_Escape:
            self.close()
        super().keyPressEvent(event)


# =============================================================================
# MAIN
# =============================================================================

def main():
    # High DPI support
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setStyle('Fusion')

    # Prefer Win11's variable font, fallback to standard Segoe UI
    font_family = "Segoe UI"
    available = QFontDatabase.families()
    if "Segoe UI Variable" in available:
        font_family = "Segoe UI Variable"
    font = QFont(font_family, 10)
    font.setHintingPreference(QFont.PreferNoHinting)
    app.setFont(font)

    overlay = ModernOverlay()
    overlay.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
