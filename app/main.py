#!/usr/bin/env python3
"""
InkyPi Wingfoil Forecast Service
================================

A comprehensive weather and wingfoil condition service for InkyPi displays.
Fetches marine weather data, evaluates wingfoil conditions, and provides
API endpoints for InkyPi integration.

Author: InkyPi Community
License: GPL-3.0 (same as InkyPi project)
"""

import os
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Any, Tuple
import requests
from flask import Flask, jsonify, render_template, request, send_from_directory
from dataclasses import dataclass, asdict
import asyncio
import aiohttp
from concurrent.futures import ThreadPoolExecutor
from dateutil import parser as dateparser
import hashlib
from threading import Lock

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@dataclass
class CachedData:
    """Data structure for cached weather data"""
    data: Dict[str, Any]
    timestamp: datetime
    data_hash: str
    model: str = ""
    data_type: str = ""  # "marine", "standard", "openweather"

    def is_stale(self, max_age_minutes: int) -> bool:
        """Check if cached data is stale"""
        age = datetime.now() - self.timestamp
        return age.total_seconds() > (max_age_minutes * 60)

    def is_valid(self, max_age_hours: int = 2) -> bool:
        """Check if cached data is still valid (not too old)"""
        age = datetime.now() - self.timestamp
        return age.total_seconds() < (max_age_hours * 3600)

app = Flask(__name__, static_folder='/app/static')
# Limit upload payloads (defense-in-depth)
app.config['MAX_CONTENT_LENGTH'] = 8 * 1024 * 1024  # 8MB

# ---------------------------------------------------------------------------
# Gustiness penalty configuration (adjust here as needed)
# Thresholds are inclusive upper-bounds for gust factor buckets
GUST_FACTOR_THRESHOLDS = [1.10, 1.25, 1.40, 1.60]
GUST_PENALTIES = [0, 1, 5, 10]  # Corresponding penalties per threshold
GUST_LABELS = [
    "steady",
    "moderately gusty",
    "gusty",
    "very gusty",
    "extremely gusty"  # Fallback label for > last threshold
]

def compute_gust_penalty_and_label(gust_factor: float) -> tuple[int, str]:
    """Return (penalty_points, label) for a given gust factor using configured buckets."""
    try:
        if gust_factor <= GUST_FACTOR_THRESHOLDS[0]:
            return GUST_PENALTIES[0], GUST_LABELS[0]
        if gust_factor <= GUST_FACTOR_THRESHOLDS[1]:
            return GUST_PENALTIES[1], GUST_LABELS[1]
        if gust_factor <= GUST_FACTOR_THRESHOLDS[2]:
            return GUST_PENALTIES[2], GUST_LABELS[2]
        if gust_factor <= GUST_FACTOR_THRESHOLDS[3]:
            return GUST_PENALTIES[3], GUST_LABELS[3]
        return 40, GUST_LABELS[4]
    except Exception:
        return 0, GUST_LABELS[0]

@app.route('/api/spot-map', methods=['POST'])
def upload_spot_map():
    try:
        # Optional admin token enforcement for mutating endpoint
        if not _require_admin(request):
            return jsonify({"error": "Unauthorized"}), 401
        if 'file' not in request.files:
            return jsonify({"error": "No file part"}), 400
        f = request.files['file']
        if not f or f.filename == '':
            return jsonify({"error": "No selected file"}), 400
        # Simple extension/type allowlist
        filename_l = f.filename.lower()
        if not (filename_l.endswith('.jpg') or filename_l.endswith('.jpeg') or filename_l.endswith('.png')):
            return jsonify({"error": "Only JPG/PNG allowed"}), 400
        # Save to Flask's static folder (mounted to /data/static on host)
        static_dir = app.static_folder or os.path.join(os.path.dirname(__file__), 'static')
        os.makedirs(static_dir, exist_ok=True)
        path = os.path.join(static_dir, 'spot-map.jpg')
        f.save(path)
        
        return jsonify({"message": "Map uploaded", "path": "/spot-map"})
    except Exception as e:
        logger.error(f"Error uploading spot map: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/spot-map')
def serve_spot_map():
    try:
        import os
        # Use Flask's static folder (now configured to /data/static)
        static_dir = app.static_folder or os.path.join(os.path.dirname(__file__), 'static')
        file_path = os.path.join(static_dir, 'spot-map.jpg')
        
        if os.path.exists(file_path):
            return send_from_directory(static_dir, 'spot-map.jpg')
        
        # Fallback to legacy location from early versions
        legacy_dir = '/app/static'
        legacy_path = os.path.join(legacy_dir, 'spot-map.jpg')
        if os.path.exists(legacy_path):
            return send_from_directory(legacy_dir, 'spot-map.jpg')
            
        return jsonify({"error": "map not found"}), 404
    except Exception as e:
        logger.error(f"Error serving spot map: {e}")
        return jsonify({"error": str(e)}), 500

# Robust asset serving for compass rose across environments
@app.route('/assets/compassrose.svg')
def serve_compass_rose():
    try:
        # Prefer runtime static dir (bind-mounted in Docker), e.g. /app/static/overlays
        static_dir = app.static_folder or os.path.join(os.path.dirname(__file__), 'static')
        primary_dir = os.path.join(static_dir, 'overlays')
        primary_file = os.path.join(primary_dir, 'compassrose.svg')
        if os.path.exists(primary_file):
            return send_from_directory(primary_dir, 'compassrose.svg')

        # Fallback: serve from package static (repo path /app/app/static/overlays)
        pkg_dir = os.path.join(os.path.dirname(__file__), 'static', 'overlays')
        if os.path.exists(os.path.join(pkg_dir, 'compassrose.svg')):
            return send_from_directory(pkg_dir, 'compassrose.svg')

        return jsonify({"error": "compassrose.svg not found"}), 404
    except Exception as e:
        logger.error(f"Error serving compassrose.svg: {e}")
        return jsonify({"error": str(e)}), 500

@dataclass
class WeatherConditions:
    """Data class for weather conditions"""
    timestamp: str
    location: str
    latitude: float
    longitude: float
    wind_speed_ms: float
    wind_speed_knots: float
    wind_direction: int
    wind_gust_ms: float
    temperature: float
    water_temperature: float
    wave_height: float
    wave_period: float
    wave_direction: int
    pressure: float
    humidity: int
    visibility: float
    uv_index: float
    precipitation_mm: float
    # Derived sport metrics (optional)
    shore_angle_deg: int = 0
    chop_index: float = 0.0
    
@dataclass
class WingfoilConditions:
    """Data class for wingfoil evaluation"""
    suitable: bool
    score: int  # 0-100
    wind_evaluation: str
    wave_evaluation: str
    overall_conditions: str
    recommendations: List[str]
    next_good_window: Optional[str]

class WeatherService:
    """Service for fetching and processing weather data with intelligent caching"""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.session = requests.Session()
        self.cache: Dict[str, CachedData] = {}
        self.cache_lock = Lock()
        self.api_settings = config.get('api_settings', {})
        self.cache_duration = self.api_settings.get('cache_duration_minutes', 30)
        self.max_cache_age = self.api_settings.get('max_cache_age_hours', 2)
        self.consensus_threshold = self.api_settings.get('consensus_threshold', 0.7)
        self.primary_model_only = self.api_settings.get('primary_model_only', False)
        self.fallback_to_secondary = self.api_settings.get('fallback_to_secondary', True)

    def _get_cache_key(self, lat: float, lon: float, model: str = "", data_type: str = "") -> str:
        """Generate a unique cache key for weather data"""
        key_data = f"{lat:.6f}_{lon:.6f}_{model}_{data_type}"
        return hashlib.md5(key_data.encode()).hexdigest()

    def _get_cached_data(self, lat: float, lon: float, model: str = "", data_type: str = "") -> Optional[CachedData]:
        """Get cached data if available and not stale"""
        cache_key = self._get_cache_key(lat, lon, model, data_type)

        with self.cache_lock:
            cached = self.cache.get(cache_key)
            if cached and not cached.is_stale(self.cache_duration):
                logger.info(f"Using cached data for {data_type} {model} ({cache_key[:8]})")
                return cached
            elif cached:
                # Remove stale cache entry
                del self.cache[cache_key]
        return None

    def _set_cached_data(self, lat: float, lon: float, model: str, data_type: str, data: Dict[str, Any]):
        """Store data in cache with metadata"""
        cache_key = self._get_cache_key(lat, lon, model, data_type)
        data_hash = hashlib.md5(json.dumps(data, sort_keys=True).encode()).hexdigest()

        cached_data = CachedData(
            data=data,
            timestamp=datetime.now(),
            data_hash=data_hash,
            model=model,
            data_type=data_type
        )

        with self.cache_lock:
            self.cache[cache_key] = cached_data
            logger.info(f"Cached {data_type} data for {model} ({cache_key[:8]})")

    def _should_fetch_model(self, model: str, primary_models: List[str]) -> bool:
        """Determine if we should fetch a specific model based on intelligent logic"""
        if self.primary_model_only and model not in primary_models[:1]:
            return False

        # Always fetch high-priority models (KNMI, DWD)
        if model in ['knmi_harmonie_arome_nl', 'dwd_icon_d2']:
            return True

        # For other models, use consensus logic - fetch only if we don't have recent data
        return True  # Simplified for now, can be enhanced later

    def _get_consensus_data(self, model_data: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        """Create consensus data from multiple models"""
        if not model_data:
            return {}

        # Use weighted average based on model weights
        model_weights = self.config.get('model_weights', {})

        # For now, return the highest-weighted model's data
        best_model = max(model_data.keys(), key=lambda m: model_weights.get(m, 1))
        return model_data[best_model]
        
    def fetch_marine_weather(self, lat: float, lon: float, retries: int = 2) -> Dict[str, Any]:
        """Fetch marine weather data from Open-Meteo Marine API with intelligent caching"""

        # Check cache first
        cached = self._get_cached_data(lat, lon, "marine", "marine")
        if cached and cached.is_valid(self.max_cache_age):
            return cached.data

        url = "https://marine-api.open-meteo.com/v1/marine"

        params = {
            "latitude": lat,
            "longitude": lon,
            "hourly": "wave_height,wave_direction,wave_period,wind_wave_height,wind_wave_direction,wind_wave_period,swell_wave_height,swell_wave_direction,swell_wave_period",
            "daily": "wave_height_max,wave_direction_dominant,wave_period_max",
            "timezone": "auto",
            "forecast_days": 5
        }

        for attempt in range(retries + 1):
            try:
                logger.info(f"Fetching marine weather (attempt {attempt + 1})")
                response = self.session.get(url, params=params, timeout=self.api_settings.get('timeout_seconds', 15))
                response.raise_for_status()

                data = response.json()
                if not self._validate_marine_data(data):
                    raise ValueError("Invalid marine data structure")

                # Check if we got valid wave data (not all None)
                wave_heights = data.get('hourly', {}).get('wave_height', [])
                if wave_heights and any(h is not None for h in wave_heights[:5]):
                    logger.info("Successfully fetched marine weather data")
                    # Cache the result
                    self._set_cached_data(lat, lon, "marine", "marine", data)
                    return data
                else:
                    logger.warning("Marine API returned None values for wave data - trying coastal fallback")
                    raise ValueError("Invalid wave data - all None values")
                
            except requests.exceptions.Timeout:
                logger.warning(f"Marine API timeout (attempt {attempt + 1})")
            except requests.exceptions.ConnectionError:
                logger.warning(f"Marine API connection error (attempt {attempt + 1})")
            except requests.exceptions.HTTPError as e:
                logger.error(f"Marine API HTTP error: {e}")
                break  # Don't retry on HTTP errors
            except Exception as e:
                logger.error(f"Error fetching marine weather (attempt {attempt + 1}): {e}")
                
            if attempt < retries:
                import time
                time.sleep(2 ** attempt)  # Exponential backoff
        
        logger.error("Failed to fetch marine weather after all retries")
        return self._get_fallback_marine_data()
        
    def _validate_marine_data(self, data: Dict[str, Any]) -> bool:
        """Validate marine weather data structure"""
        required_keys = ['hourly']
        if not all(key in data for key in required_keys):
            return False
        
        hourly = data.get('hourly', {})
        required_hourly = ['time', 'wave_height']
        return all(key in hourly for key in required_hourly)
    
    def _get_fallback_marine_data(self) -> Dict[str, Any]:
        """Return fallback marine data when API fails - now tries coastal location"""
        from datetime import datetime, timedelta
        base_time = datetime.now()
        times = [(base_time + timedelta(hours=i)).isoformat() for i in range(24)]
        
        # Try to get marine data from a nearby coastal location (IJmuiden)
        try:
            coastal_params = {
                "latitude": 52.4601,  # IJmuiden coordinates
                "longitude": 4.5747,
                "hourly": "wave_height,wave_direction,wave_period,wind_wave_height,wind_wave_direction,wind_wave_period,swell_wave_height,swell_wave_direction,swell_wave_period",
                "daily": "wave_height_max,wave_direction_dominant,wave_period_max",
                "timezone": "auto",
                "forecast_days": 5
            }
            coastal_response = requests.get(
                "https://marine-api.open-meteo.com/v1/marine",
                params=coastal_params,
                timeout=10
            )
            
            if coastal_response.status_code == 200:
                coastal_data = coastal_response.json()
                if self._validate_marine_data(coastal_data):
                    logger.info("Using coastal marine data as fallback")
                    return coastal_data
        except Exception as e:
            logger.warning(f"Failed to fetch coastal marine data: {e}")
        
        # If coastal data also fails, return minimal fallback
        logger.warning("Using minimal marine fallback data")
        return {
            "hourly": {
                "time": times,
                "wave_height": [0.1] * 24,  # Minimal wave height for inland waters
                "wave_period": [3.0] * 24,
                "wave_direction": [180] * 24,
                "wind_wave_height": [0.1] * 24,
                "swell_wave_height": [0.0] * 24,
                "wind_wave_period": [3.0] * 24,
                "swell_wave_period": [0.0] * 24
            }
        }
    
    def fetch_standard_weather(self, lat: float, lon: float, retries: int = 2, model: str = "") -> Dict[str, Any]:
        """Fetch standard weather data from Open-Meteo with intelligent caching and model selection"""

        # Check cache first
        cached = self._get_cached_data(lat, lon, model, "standard")
        if cached and cached.is_valid(self.max_cache_age):
            return cached.data

        url = "https://api.open-meteo.com/v1/forecast"

        # Determine which model to use
        models_to_try = [model] if model else self.config.get('models', ['knmi_harmonie_arome_nl', 'dwd_icon_d2'])

        for attempt in range(retries + 1):
            for current_model in models_to_try:
                try:
                    logger.info(f"Fetching standard weather for model {current_model} (attempt {attempt + 1})")

                    params = {
                        "latitude": lat,
                        "longitude": lon,
                        "current": "temperature_2m,wind_speed_10m,wind_gusts_10m,wind_direction_10m,uv_index",
                        "hourly": "temperature_2m,relative_humidity_2m,pressure_msl,wind_speed_10m,wind_direction_10m,wind_gusts_10m,visibility,uv_index,precipitation",
                        "daily": "temperature_2m_max,temperature_2m_min,wind_speed_10m_max,wind_gusts_10m_max",
                        "wind_speed_unit": "ms",
                        "timezone": "auto",
                        "forecast_days": 5
                    }

                    # Note: Open-Meteo API doesn't support model selection via 'models' parameter
                    # We'll use the default model and handle different data sources separately

                    response = self.session.get(url, params=params, timeout=self.api_settings.get('timeout_seconds', 15))
                    response.raise_for_status()

                    data = response.json()
                    if not self._validate_standard_data(data):
                        raise ValueError(f"Invalid standard weather data structure for {current_model}")

                    logger.info(f"Successfully fetched standard weather data for {current_model}")

                    # Cache the result
                    self._set_cached_data(lat, lon, current_model, "standard", data)
                    return data

                except requests.exceptions.Timeout:
                    logger.warning(f"Standard weather API timeout (attempt {attempt + 1})")
                except requests.exceptions.ConnectionError:
                    logger.warning(f"Standard weather API connection error (attempt {attempt + 1})")
                except requests.exceptions.HTTPError as e:
                    logger.error(f"Standard weather API HTTP error: {e}")
                    break
                except Exception as e:
                    logger.error(f"Error fetching standard weather (attempt {attempt + 1}): {e}")

            if attempt < retries:
                import time
                time.sleep(2 ** attempt)
        
        logger.error("Failed to fetch standard weather after all retries")
        return self._get_fallback_standard_data()
    
    def _validate_standard_data(self, data: Dict[str, Any]) -> bool:
        """Validate standard weather data structure"""
        required_keys = ['hourly']
        if not all(key in data for key in required_keys):
            return False
        
        hourly = data.get('hourly', {})
        required_hourly = ['time', 'wind_speed_10m', 'temperature_2m']
        return all(key in hourly for key in required_hourly)
    
    def _get_fallback_standard_data(self) -> Dict[str, Any]:
        """Return fallback standard weather data when API fails - tries alternative models"""
        from datetime import datetime, timedelta
        base_time = datetime.now()
        times = [(base_time + timedelta(hours=i)).isoformat() for i in range(24)]
        
        # Try to get data from default Open-Meteo model as fallback
        try:
            url = "https://api.open-meteo.com/v1/forecast"
            params = {
                "latitude": 51.8781,  # Default location
                "longitude": 5.8654,
                "current": "temperature_2m,wind_speed_10m,wind_direction_10m,wind_gusts_10m,uv_index",
                "hourly": "temperature_2m,wind_speed_10m,wind_direction_10m,wind_gusts_10m,relative_humidity_2m,pressure_msl,visibility,uv_index,precipitation",
                "forecast_days": 5
            }
            
            response = requests.get(url, params=params, timeout=10)
            if response.status_code == 200:
                data = response.json()
                if self._validate_standard_data(data):
                    logger.info("Using default Open-Meteo data as fallback")
                    return data
        except Exception as e:
            logger.warning(f"Failed to fetch default fallback data: {e}")
        
        # If all fallbacks fail, return minimal realistic data
        logger.warning("Using minimal standard fallback data")
        return {
            "current": {
                "temperature_2m": 15.0,  # More realistic for Netherlands
                "wind_speed_10m": 8.0,
                "wind_gusts_10m": 12.0,
                "wind_direction_10m": 180,
                "uv_index": 2.0
            },
            "hourly": {
                "time": times,
                "temperature_2m": [15.0] * 24,
                "wind_speed_10m": [8.0] * 24,
                "wind_direction_10m": [180] * 24,
                "wind_gusts_10m": [12.0] * 24,
                "relative_humidity_2m": [70] * 24,
                "pressure_msl": [1013.0] * 24,
                "visibility": [10000.0] * 24,
                "uv_index": [2.0] * 24
            },
            "utc_offset_seconds": 0
        }

    def fetch_openweather(self, lat: float, lon: float, api_key: Optional[str], retries: int = 1) -> Optional[Dict[str, Any]]:
        """Optional: fetch current wind via OpenWeather if API key provided (for cross-check) with caching"""
        if not api_key:
            logger.info("No OpenWeather API key provided")
            return None

        # Check cache first
        cached = self._get_cached_data(lat, lon, "openweather", "current")
        if cached and cached.is_valid(self.max_cache_age):
            return cached.data

        url = "https://api.openweathermap.org/data/2.5/weather"
        params = {"lat": lat, "lon": lon, "appid": api_key, "units": "metric"}

        for attempt in range(retries + 1):
            try:
                logger.info(f"Fetching OpenWeather data (attempt {attempt + 1})")
                r = self.session.get(url, params=params, timeout=self.api_settings.get('timeout_seconds', 15))
                r.raise_for_status()

                data = r.json()
                if not self._validate_openweather_data(data):
                    raise ValueError("Invalid OpenWeather data structure")

                logger.info("Successfully fetched OpenWeather data")

                # Cache the result
                self._set_cached_data(lat, lon, "openweather", "current", data)
                return data
                
            except requests.exceptions.Timeout:
                logger.warning(f"OpenWeather API timeout (attempt {attempt + 1})")
            except requests.exceptions.HTTPError as e:
                logger.warning(f"OpenWeather API HTTP error: {e}")
                if e.response.status_code == 401:
                    logger.error("OpenWeather API key invalid")
                    break
            except Exception as e:
                logger.warning(f"OpenWeather fetch failed (attempt {attempt + 1}): {e}")
                
            if attempt < retries:
                import time
                time.sleep(1)
                
        logger.warning("Failed to fetch OpenWeather data after all retries")
        return None
    
    def _validate_openweather_data(self, data: Dict[str, Any]) -> bool:
        """Validate OpenWeather data structure"""
        required_keys = ['wind']
        if not all(key in data for key in required_keys):
            return False
        
        wind = data.get('wind', {})
        return 'speed' in wind

    def fetch_standard_weather_models(self, lat: float, lon: float, models: List[str]) -> Dict[str, Dict[str, Any]]:
        """Fetch standard weather for multiple models with intelligent caching and prioritization"""
        results: Dict[str, Dict[str, Any]] = {}

        # Prioritize high-resolution models first
        priority_models = ['knmi_harmonie_arome_nl', 'dwd_icon_d2']
        ordered_models = [m for m in priority_models if m in models] + [m for m in models if m not in priority_models]

        for model in ordered_models:
            # Check if we should fetch this model
            if not self._should_fetch_model(model, priority_models):
                logger.info(f"Skipping model {model} due to optimization settings")
                continue

            # Check cache first
            cached = self._get_cached_data(lat, lon, model, "standard")
            if cached and cached.is_valid(self.max_cache_age):
                results[model] = cached.data
                logger.info(f"Using cached data for model {model}")
                continue

            try:
                # Use the updated fetch_standard_weather method
                data = self.fetch_standard_weather(lat, lon, model=model)
                if data:
                    results[model] = data
            except Exception as e:
                logger.warning(f"Model fetch failed for {model}: {e}")
                # Try to get fallback data from cache or other models
                if self.fallback_to_secondary:
                    # Look for any recent data from other models
                    for fallback_model in ordered_models:
                        if fallback_model != model:
                            fallback_cached = self._get_cached_data(lat, lon, fallback_model, "standard")
                            if fallback_cached and fallback_cached.is_valid(self.max_cache_age):
                                logger.info(f"Using fallback data from {fallback_model} for failed {model}")
                                results[model] = fallback_cached.data
                                break

        return results

    def fetch_openweather_forecast(self, lat: float, lon: float, api_key: Optional[str], retries: int = 1) -> Optional[Dict[str, Any]]:
        """Fetch 5 day / 3 hour forecast from OpenWeather (if API key provided).
        API docs: https://openweathermap.org/forecast5
        """
        if not api_key:
            return None
        url = "https://api.openweathermap.org/data/2.5/forecast"
        params = {"lat": lat, "lon": lon, "appid": api_key, "units": "metric"}
        for attempt in range(retries + 1):
            try:
                r = self.session.get(url, params=params, timeout=15)
                r.raise_for_status()
                data = r.json()
                if not isinstance(data, dict) or 'list' not in data:
                    raise ValueError("Invalid OpenWeather forecast data structure")
                return data
            except Exception:
                if attempt >= retries:
                    break
                import time
                time.sleep(1)
        return None
    
    def fetch_water_temperature(self, lat: float, lon: float) -> Optional[float]:
        """Fetch water temperature from marine data"""
        # For now, we'll estimate based on location and season
        # In production, you might use a dedicated sea temperature API
        import math
        
        # Simple seasonal estimation (this is a placeholder)
        day_of_year = datetime.now().timetuple().tm_yday
        seasonal_factor = math.cos((day_of_year - 172) * 2 * math.pi / 365)
        
        # Base temperature varies by latitude
        base_temp = 15 + (30 - abs(lat)) * 0.5
        water_temp = base_temp + seasonal_factor * 8
        
        return max(5, min(30, water_temp))  # Reasonable bounds

class WingfoilAnalyzerV2:
    """Advanced wingfoil condition analyzer using 4-pillar scoring system with dynamic model blending"""
    
    def __init__(self, preferences: Dict[str, Any], config: Dict[str, Any]):
        self.preferences = preferences
        self.config = config
        self.user_weight = preferences.get('rider_weight_kg', 70)
        self.skill_level = preferences.get('skill_level', 'intermediate')

        # Load scoring algorithm configuration from config
        scoring_config = config.get('scoring_algorithm', {})

        # Skill-specific wind targets (m/s) - read from config with defaults
        default_targets = {
            'beginner': {'min': 6, 'opt_min': 9, 'opt_max': 13, 'max': 16},
            'intermediate': {'min': 7, 'opt_min': 10, 'opt_max': 15, 'max': 18},
            'advanced': {'min': 8, 'opt_min': 11, 'opt_max': 18, 'max': 22}
        }
        skill_targets = scoring_config.get('skill_targets', default_targets)
        self.wind_targets = {}
        for skill in ['beginner', 'intermediate', 'advanced']:
            target_config = skill_targets.get(skill, default_targets[skill])
            self.wind_targets[skill] = {
                'min': target_config.get('min_wind_ms', default_targets[skill]['min']),
                'opt_min': target_config.get('opt_min_wind_ms', default_targets[skill]['opt_min']),
                'opt_max': target_config.get('opt_max_wind_ms', default_targets[skill]['opt_max']),
                'max': target_config.get('max_wind_ms', default_targets[skill]['max'])
            }

        # Pillar weights - read from config with defaults
        default_weights = {'wind': 0.60, 'surface': 0.20, 'safety': 0.15, 'comfort': 0.05}
        self.pillar_weights = scoring_config.get('pillar_weights', default_weights)

        # Scoring parameters - read from config with defaults
        scoring_params = scoring_config.get('scoring_parameters', {})
        self.scoring_params = {
            'wind_consistency_penalty': scoring_params.get('wind_consistency_penalty', 8),
            'surface_flatwater_penalty': scoring_params.get('surface_flatwater_penalty', 2.0),
            'surface_height_penalty': scoring_params.get('surface_height_penalty', 3.0),
            'surface_chop_penalty': scoring_params.get('surface_chop_penalty', 1.5),
            'safety_gust_penalty': scoring_params.get('safety_gust_penalty', 8)
        }
        
    def calculate_wind_pillar(self, wind_speed_ms: float, wind_gust_ms: float, 
                             wind_direction: float, shore_direction: float) -> Dict[str, Any]:
        """Calculate wind pillar score using v2 algorithm"""
        import math
        
        targets = self.wind_targets[self.skill_level]
        V = wind_speed_ms
        
        # 2.1 Speed scoring (Gaussian curve)
        if V <= targets['min'] or V >= targets['max']:
            speed_score = 0.0
        else:
            V_star = (targets['opt_min'] + targets['opt_max']) / 2
            sigma = 0.18 * (targets['max'] - targets['min'])
            speed_score = math.exp(-((V - V_star) ** 2) / (2 * sigma ** 2))
        
        # 2.2 Direction scoring (shore alignment)
        delta = abs(wind_direction - shore_direction)
        if delta > 180:
            delta = 360 - delta
            
        if delta <= 90:
            direction_score = math.cos(math.radians((delta - 45) / 45)) ** 2
        else:
            direction_score = 0.3 * math.cos(math.radians((delta - 135) / 45)) ** 2
            
        # Cap offshore winds for beginners
        if delta < 30 and self.skill_level == 'beginner':
            direction_score = min(direction_score, 0.6)
        
        # 2.3 Consistency scoring (gust and lull) - configurable penalty
        gust_factor = wind_gust_ms / max(wind_speed_ms, 0.1)
        k_g = self.scoring_params['wind_consistency_penalty']
        consistency_score = math.exp(-k_g * max(0, gust_factor - 1.15) ** 2)
        
        # Combined wind score
        wind_score = 0.55 * speed_score + 0.20 * direction_score + 0.25 * consistency_score
        
        return {
            'score': wind_score,
            'speed_score': speed_score,
            'direction_score': direction_score,
            'consistency_score': consistency_score,
            'gust_factor': gust_factor,
            'wind_speed_ms': wind_speed_ms,
            'wind_direction': wind_direction
        }
    
    def calculate_surface_pillar(self, wave_height: float, wave_period: float, 
                               wind_speed_ms: float, current_speed: float = 0.0) -> Dict[str, Any]:
        """Calculate surface pillar score using v2 algorithm"""
        import math
        
        # Handle None values
        if wave_height is None:
            wave_height = 0.1
        if wave_period is None:
            wave_period = 3.0
            
        Hs = wave_height
        Tp = wave_period
        
        # 3.1 Wave Quality (assuming flatwater preset for now)
        steepness = Hs / (Tp ** 2) if Tp > 0 else 0
        c_f = self.scoring_params['surface_flatwater_penalty']
        d_f = self.scoring_params['surface_height_penalty']

        wave_quality = math.exp(-c_f * steepness) * math.exp(-d_f * max(0, Hs - 0.6))

        # 3.2 Chop penalty
        c_c = self.scoring_params['surface_chop_penalty']
        d_c = 0.1
        chop_score = math.exp(-c_c * steepness) * math.exp(-d_c * wind_speed_ms * abs(current_speed))
        
        # 3.3 Current/Tide (simplified logistic curve)
        current_score = 1.0 / (1.0 + math.exp(-5 * (current_speed - 0.5)))
        
        # Combined surface score
        surface_score = 0.55 * wave_quality + 0.25 * chop_score + 0.20 * current_score
        
        return {
            'score': surface_score,
            'wave_quality': wave_quality,
            'chop_score': chop_score,
            'current_score': current_score,
            'wave_height': Hs,
            'wave_period': Tp,
            'steepness': steepness
        }
    
    def calculate_safety_pillar(self, gust_factor: float, temperature: float, 
                              visibility: float, precipitation: float = 0.0) -> Dict[str, Any]:
        """Calculate safety pillar score using v2 algorithm"""
        import math
        
        # Base safety from consistency (gust factor) - configurable penalty
        base_safety = math.exp(-self.scoring_params['safety_gust_penalty'] * max(0, gust_factor - 1.15) ** 2)
        
        # Weather penalties
        weather_factor = 1.0
        if precipitation >= 8:  # Heavy rain
            weather_factor *= 0.4
        elif precipitation >= 4:  # Moderate rain
            weather_factor *= 0.7
            
        # Wind-chill effect (simplified)
        wind_chill_factor = 1.0
        if temperature <= -10:
            wind_chill_factor = 0.3
        elif temperature <= -5:
            wind_chill_factor = 0.6
            
        # Visibility penalty
        visibility_factor = 1.0
        if visibility < 1000:  # Poor visibility
            visibility_factor = 0.5
            
        safety_score = base_safety * weather_factor * wind_chill_factor * visibility_factor
        
        return {
            'score': safety_score,
            'base_safety': base_safety,
            'weather_factor': weather_factor,
            'wind_chill_factor': wind_chill_factor,
            'visibility_factor': visibility_factor,
            'gust_factor': gust_factor
        }
    
    def calculate_comfort_pillar(self, temperature: float, precipitation: float = 0.0, 
                               humidity: float = 50.0) -> Dict[str, Any]:
        """Calculate comfort pillar score using v2 algorithm"""
        import math
        
        # Temperature comfort (sigmoid curve)
        temp_score = 1.0 / (1.0 + math.exp(-0.2 * (temperature - 20)))
        
        # Precipitation penalty
        rain_score = math.exp(-0.5 * precipitation)
        
        # Humidity penalty (simplified)
        humidity_score = 1.0 - 0.3 * max(0, abs(humidity - 50) / 50)
        
        comfort_score = (temp_score + rain_score + humidity_score) / 3.0
        
        return {
            'score': comfort_score,
            'temp_score': temp_score,
            'rain_score': rain_score,
            'humidity_score': humidity_score,
            'temperature': temperature
        }
    
    def calculate_model_confidence(self, model_data: Dict[str, Any]) -> float:
        """Calculate confidence score based on model spread and data quality"""
        import math
        
        # Simplified confidence calculation
        # In a full implementation, this would analyze model spread, data age, etc.
        confidence = 0.75  # Base confidence
        
        # Adjust based on data freshness and model agreement
        if model_data.get('models_used', 0) >= 3:
            confidence += 0.1
        if model_data.get('spread_knots', 0) < 2.0:
            confidence += 0.1
            
        return min(1.0, confidence)
    
    def _generate_score_breakdown(self, wind_pillar: Dict[str, Any], surface_pillar: Dict[str, Any],
                                safety_pillar: Dict[str, Any], comfort_pillar: Dict[str, Any],
                                weighted_contributions: Dict[str, float], confidence: float,
                                wind_speed_ms: float, wind_gust_ms: float, wind_direction: float,
                                shore_direction: float, weather: WeatherConditions) -> Dict[str, Any]:
        """Generate detailed score breakdown with formulas and value mappings"""
        
        # Get skill-specific wind targets for display
        targets = self.wind_targets[self.skill_level]
        
        # Calculate wind direction angle difference
        delta = abs(wind_direction - shore_direction)
        if delta > 180:
            delta = 360 - delta
            
        # Wind direction interpretation
        if delta <= 30:
            direction_desc = "Offshore"
        elif delta <= 60:
            direction_desc = "Cross-offshore"
        elif delta <= 90:
            direction_desc = "Cross-shore"
        elif delta <= 120:
            direction_desc = "Cross-onshore"
        else:
            direction_desc = "Onshore"
            
        # Gust factor interpretation
        gust_factor = wind_pillar['gust_factor']
        if gust_factor <= 1.10:
            gust_desc = "Steady"
        elif gust_factor <= 1.25:
            gust_desc = "Moderately gusty"
        elif gust_factor <= 1.40:
            gust_desc = "Gusty"
        elif gust_factor <= 1.60:
            gust_desc = "Very gusty"
        else:
            gust_desc = "Extremely gusty"
            
        # Wave steepness calculation
        steepness = surface_pillar['steepness']
        
        # Overall formula explanation
        overall_formula = "Overall = clamp(100 × (0.60×Wind + 0.20×Surface + 0.15×Safety + 0.05×Comfort) × Confidence, 0, 100)"
        
        return {
            'formula': {
                'overall': overall_formula,
                'wind': "Wind = 0.55×Speed + 0.20×Direction + 0.25×Consistency",
                'surface': "Surface = 0.55×WaveQuality + 0.25×Chop + 0.20×Current",
                'safety': "Safety = Base × Weather × WindChill × Visibility",
                'comfort': "Comfort = (Temp + Rain + Humidity) / 3"
            },
            'current_values': {
                'wind_speed_ms': round(wind_speed_ms, 2),
                'wind_speed_knots': round(wind_speed_ms * 1.944, 1),
                'wind_gust_ms': round(wind_gust_ms, 2),
                'wind_gust_knots': round(wind_gust_ms * 1.944, 1),
                'wind_direction': int(wind_direction),
                'shore_direction': int(shore_direction),
                'direction_angle_diff': int(delta),
                'gust_factor': round(gust_factor, 3),
                'wave_height': round(weather.wave_height, 2),
                'wave_period': round(weather.wave_period, 1),
                'wave_steepness': round(steepness, 4),
                'temperature': round(weather.temperature, 1),
                'visibility': int(weather.visibility),
                'precipitation_mm': round(getattr(weather, 'precipitation_mm', 0.0), 2),
                'humidity': int(getattr(weather, 'humidity', 50)),
                'confidence': round(confidence, 3)
            },
            'skill_level': {
                'current': self.skill_level,
                'wind_targets': {
                    'min_ms': targets['min'],
                    'opt_min_ms': targets['opt_min'],
                    'opt_max_ms': targets['opt_max'],
                    'max_ms': targets['max'],
                    'min_knots': round(targets['min'] * 1.944, 1),
                    'opt_min_knots': round(targets['opt_min'] * 1.944, 1),
                    'opt_max_knots': round(targets['opt_max'] * 1.944, 1),
                    'max_knots': round(targets['max'] * 1.944, 1)
                }
            },
            'interpretations': {
                'wind_direction': direction_desc,
                'gust_factor': gust_desc,
                'wind_speed_category': self._categorize_wind_speed(wind_speed_ms, targets),
                'wave_category': self._categorize_wave_height(weather.wave_height),
                'temperature_category': self._categorize_temperature(weather.temperature)
            },
            'pillar_details': {
                'wind': {
                    'speed_score': round(wind_pillar['speed_score'], 3),
                    'direction_score': round(wind_pillar['direction_score'], 3),
                    'consistency_score': round(wind_pillar['consistency_score'], 3),
                    'total_score': round(wind_pillar['score'], 3),
                    'weighted_contribution': round(weighted_contributions['wind'], 3)
                },
                'surface': {
                    'wave_quality': round(surface_pillar['wave_quality'], 3),
                    'chop_score': round(surface_pillar['chop_score'], 3),
                    'current_score': round(surface_pillar['current_score'], 3),
                    'total_score': round(surface_pillar['score'], 3),
                    'weighted_contribution': round(weighted_contributions['surface'], 3)
                },
                'safety': {
                    'base_safety': round(safety_pillar['base_safety'], 3),
                    'weather_factor': round(safety_pillar['weather_factor'], 3),
                    'wind_chill_factor': round(safety_pillar['wind_chill_factor'], 3),
                    'visibility_factor': round(safety_pillar['visibility_factor'], 3),
                    'total_score': round(safety_pillar['score'], 3),
                    'weighted_contribution': round(weighted_contributions['safety'], 3)
                },
                'comfort': {
                    'temp_score': round(comfort_pillar['temp_score'], 3),
                    'rain_score': round(comfort_pillar['rain_score'], 3),
                    'humidity_score': round(comfort_pillar['humidity_score'], 3),
                    'total_score': round(comfort_pillar['score'], 3),
                    'weighted_contribution': round(weighted_contributions['comfort'], 3)
                }
            }
        }
    
    def _categorize_wind_speed(self, wind_speed_ms: float, targets: Dict[str, float]) -> str:
        """Categorize wind speed based on skill level targets"""
        if wind_speed_ms < targets['min']:
            return "Too light"
        elif wind_speed_ms < targets['opt_min']:
            return "Light"
        elif wind_speed_ms <= targets['opt_max']:
            return "Optimal"
        elif wind_speed_ms <= targets['max']:
            return "Strong"
        else:
            return "Too strong"
    
    def _categorize_wave_height(self, wave_height: float) -> str:
        """Categorize wave height"""
        if wave_height < 0.2:
            return "Flat"
        elif wave_height < 0.5:
            return "Small chop"
        elif wave_height < 1.0:
            return "Moderate"
        elif wave_height < 1.5:
            return "Large"
        else:
            return "Very large"
    
    def _categorize_temperature(self, temperature: float) -> str:
        """Categorize temperature"""
        if temperature < 5:
            return "Very cold"
        elif temperature < 10:
            return "Cold"
        elif temperature < 15:
            return "Cool"
        elif temperature < 25:
            return "Mild"
        else:
            return "Warm"
    
    def analyze_conditions_v2(self, weather: WeatherConditions, shore_direction: int = 90, 
                            model_data: Dict[str, Any] = None) -> Dict[str, Any]:
        """Analyze conditions using v2 4-pillar scoring system"""
        
        # Convert to metric units
        wind_speed_ms = weather.wind_speed_knots * 0.514444
        wind_gust_ms = weather.wind_gust_ms
        wind_direction = weather.wind_direction
        
        # Calculate pillar scores
        wind_pillar = self.calculate_wind_pillar(wind_speed_ms, wind_gust_ms, wind_direction, shore_direction)
        surface_pillar = self.calculate_surface_pillar(weather.wave_height, weather.wave_period, wind_speed_ms)
        safety_pillar = self.calculate_safety_pillar(
            wind_pillar['gust_factor'],
            weather.temperature,
            weather.visibility,
            getattr(weather, 'precipitation_mm', 0.0)
        )
        comfort_pillar = self.calculate_comfort_pillar(
            weather.temperature,
            getattr(weather, 'precipitation_mm', 0.0),
            getattr(weather, 'humidity', 50.0)
        )
        
        # Calculate confidence
        confidence = self.calculate_model_confidence(model_data or {})
        
        # Calculate overall score
        overall_score = (
            self.pillar_weights['wind'] * wind_pillar['score'] +
            self.pillar_weights['surface'] * surface_pillar['score'] +
            self.pillar_weights['safety'] * safety_pillar['score'] +
            self.pillar_weights['comfort'] * comfort_pillar['score']
        ) * confidence
        
        # Clamp to 0-100 range
        overall_score = max(0, min(100, overall_score * 100))
        
        # Determine suitability and conditions
        suitable = overall_score >= 60
        if overall_score >= 85:
            conditions = "Excellent"
        elif overall_score >= 70:
            conditions = "Good"
        elif overall_score >= 60:
            conditions = "Marginal"
        else:
            conditions = "Poor"
        
        # Calculate weighted pillar contributions
        weighted_contributions = {
            'wind': self.pillar_weights['wind'] * wind_pillar['score'] * confidence,
            'surface': self.pillar_weights['surface'] * surface_pillar['score'] * confidence,
            'safety': self.pillar_weights['safety'] * safety_pillar['score'] * confidence,
            'comfort': self.pillar_weights['comfort'] * comfort_pillar['score'] * confidence
        }
        
        # Generate detailed score breakdown with formulas and mappings
        score_breakdown = self._generate_score_breakdown(
            wind_pillar, surface_pillar, safety_pillar, comfort_pillar,
            weighted_contributions, confidence, wind_speed_ms, wind_gust_ms,
            wind_direction, shore_direction, weather
        )
        
        return {
            'overall_score': int(overall_score),
            'suitable': suitable,
            'conditions': conditions,
            'confidence': confidence,
            'pillars': {
                'wind': wind_pillar,
                'surface': surface_pillar,
                'safety': safety_pillar,
                'comfort': comfort_pillar
            },
            'weights': self.pillar_weights,
            'weighted_contributions': weighted_contributions,
            'skill_level': self.skill_level,
            'score_breakdown': score_breakdown,
            'computation_trace': {
                'wind_speed_ms': wind_speed_ms,
                'wind_gust_ms': wind_gust_ms,
                'wind_direction': wind_direction,
                'shore_direction': shore_direction,
                'wave_height': weather.wave_height,
                'wave_period': weather.wave_period,
                'temperature': weather.temperature,
                'visibility': weather.visibility,
                'gust_factor': wind_pillar['gust_factor']
            }
        }
    
    # Legacy method removed - use analyze_conditions_v2 instead


class WingfoilAdvisor:
    """Provides wingfoil-specific recommendations based on conditions and rider profile"""

    def __init__(self, preferences: Dict[str, Any], user: Dict[str, Any]):
        self.preferences = preferences
        self.user = user or {}

    def recommend_wing_size(self, wind_knots: float) -> Tuple[str, List[str]]:
        weight = float(self.user.get("rider_weight_kg", 80))
        skill = (self.user.get("skill_level", "intermediate") or "intermediate").lower()

        # Enhanced wing sizing for wingfoiling (more precise ranges)
        if wind_knots < 8:
            size = "7-8m"
            wind_desc = "very light"
        elif wind_knots < 12:
            size = "6-7m"
            wind_desc = "light"
        elif wind_knots < 16:
            size = "5-6m"
            wind_desc = "moderate"
        elif wind_knots < 20:
            size = "4-5m"
            wind_desc = "fresh"
        elif wind_knots < 25:
            size = "3.5-4m"
            wind_desc = "strong"
        elif wind_knots < 30:
            size = "3-3.5m"
            wind_desc = "very strong"
        else:
            size = "2.5-3m"
            wind_desc = "extreme"

        notes: List[str] = []
        
        # Weight adjustments (more detailed)
        if weight >= 100:
            notes.append("Heavy rider (100kg+): size up 1-1.5m")
        elif weight >= 85:
            notes.append("Heavy rider (85kg+): size up 0.5-1m")
        elif weight <= 60:
            notes.append("Light rider (60kg-): size down 0.5-1m")
        elif weight <= 70:
            notes.append("Light rider (70kg-): size down 0.5m")

        # Skill adjustments
        if skill in ("beginner", "novice"):
            notes.append("Beginner: use larger stable wing, avoid gusty conditions")
        elif skill == "advanced":
            notes.append("Advanced: can handle smaller wings in marginal conditions")

        # Wind-specific advice
        if wind_knots < 10:
            notes.append(f"Light wind ({wind_desc}): use largest wing and light equipment")
        elif wind_knots > 25:
            notes.append(f"Strong wind ({wind_desc}): prioritize safety and control")

        return size, notes

    def compute_advice(self, weather: WeatherConditions) -> Dict[str, Any]:
        gust_knots = float(weather.wind_gust_ms) * 1.944
        gust_factor = (gust_knots / weather.wind_speed_knots) if weather.wind_speed_knots > 0 else 1.0
        wing_size, wing_notes = self.recommend_wing_size(weather.wind_speed_knots)
        
        # Enhanced equipment recommendations with detailed info
        foil_advice = self._get_foil_advice(weather)
        board_advice = self._get_board_advice(weather)
        
        # Session and general advice
        session_advice = self._get_session_advice(weather, gust_factor)
        general_advice = self._get_general_advice(weather, gust_factor)

        # Build combined, de-duplicated advice list at the source using topic-based consolidation
        def _category_of(text: str) -> str:
            t = (text or '').lower()
            if not t:
                return ''
            if 'gust' in t:
                return 'gusty'
            if 'too light' in t or 'light wind' in t:
                return 'light_wind'
            if 'too strong' in t or 'strong wind' in t:
                return 'strong_wind'
            if 'flat water' in t:
                return 'flat_water'
            if 'visibility' in t:
                return 'visibility'
            if 'wave' in t or 'waves' in t:
                return 'waves'
            if 'uv' in t:
                return 'uv'
            if 'cold' in t or 'cool' in t or 'warm' in t:
                return 'temperature'
            return 'general'

        def _prefer(cat: str, a: str, b: str) -> str:
            # Choose more actionable/specific advice per category
            if cat == 'gusty':
                # Prefer equipment/action guidance over generic practice
                if 'smaller wing' in (a or '').lower():
                    return a
                if 'smaller wing' in (b or '').lower():
                    return b
            if cat == 'light_wind':
                # Prefer "too light" warning if present
                if 'too light' in (a or '').lower():
                    return a
                if 'too light' in (b or '').lower():
                    return b
            # Default: keep the first one
            return a

        consolidated: Dict[str, str] = {}
        for line in ((session_advice or []) + (general_advice or [])):
            if not line:
                continue
            cat = _category_of(line)
            if not cat:
                continue
            if cat not in consolidated:
                consolidated[cat] = line
            else:
                consolidated[cat] = _prefer(cat, consolidated[cat], line)

        # Preserve a stable ordering by category priority
        category_order = ['light_wind', 'gusty', 'strong_wind', 'waves', 'flat_water', 'visibility', 'temperature', 'uv', 'general']
        combined_advice = [consolidated[c] for c in category_order if c in consolidated][:6]

        advice = {
            "recommended_wing_size": wing_size,
            "wing_notes": wing_notes,
            "foil_advice": foil_advice,
            "board_advice": board_advice,
            # Keep raw fields for compatibility but prefer combined_advice on the client
            "session_advice": session_advice,
            "general_advice": general_advice,
            "combined_advice": combined_advice,
            "gust_factor": round(gust_factor, 2),
            "conditions_summary": self._generate_conditions_summary(weather)
        }
        return advice
    
    def _get_foil_advice(self, weather: WeatherConditions) -> Dict[str, Any]:
        """Get detailed foil recommendations"""
        wind_knots = weather.wind_speed_knots
        skill = self.user.get("skill_level", "intermediate").lower()
        
        # Base foil size recommendations
        if wind_knots < 8:
            size = "1000-1400cm²"
            description = "Large front wing for light wind"
            details = "High aspect ratio foil with large surface area for early planing"
        elif wind_knots < 12:
            size = "900-1200cm²"
            description = "Medium-large front wing"
            details = "Balanced foil for light to moderate winds, good for learning"
        elif wind_knots < 18:
            size = "700-1000cm²"
            description = "Medium front wing"
            details = "Versatile foil for moderate winds, good maneuverability"
        elif wind_knots < 25:
            size = "500-800cm²"
            description = "Small-medium front wing"
            details = "High performance foil for strong winds, requires more skill"
        else:
            size = "400-600cm²"
            description = "Small front wing"
            details = "High-speed foil for very strong winds, advanced riders only"
        
        # Skill-based adjustments
        skill_notes = []
        if skill in ("beginner", "novice"):
            skill_notes.append("Beginner: Choose larger, more stable foil")
            skill_notes.append("Avoid high aspect ratio foils initially")
        elif skill == "advanced":
            skill_notes.append("Advanced: Can handle smaller, more responsive foils")
        
        # Wind-specific details
        wind_notes = []
        if wind_knots < 10:
            wind_notes.append("Light wind: Focus on early planing and stability")
        elif wind_knots > 20:
            wind_notes.append("Strong wind: Prioritize control and safety")
        
        return {
            "size": size,
            "description": description,
            "details": details,
            "skill_notes": skill_notes,
            "wind_notes": wind_notes
        }
    
    def _get_board_advice(self, weather: WeatherConditions) -> Dict[str, Any]:
        """Get detailed board recommendations"""
        wind_knots = weather.wind_speed_knots
        skill = self.user.get("skill_level", "intermediate").lower()
        weight = float(self.user.get("rider_weight_kg", 80))
        
        # Base board size recommendations
        if skill in ("beginner", "novice"):
            size = "80-120L"
            description = "Large stable board"
            details = "Wide, stable platform for learning and light wind"
        elif wind_knots < 12:
            size = "70-100L"
            description = "Medium-large board"
            details = "Good for light wind planing and learning"
        elif wind_knots < 18:
            size = "60-85L"
            description = "Medium board"
            details = "Versatile size for moderate conditions"
        elif wind_knots < 25:
            size = "50-70L"
            description = "Small-medium board"
            details = "High performance for strong winds"
        else:
            size = "40-60L"
            description = "Small board"
            details = "High-speed board for very strong winds"
        
        # Weight adjustments
        weight_notes = []
        if weight >= 90:
            weight_notes.append(f"Heavy rider ({weight}kg): Add 10-20L to recommended size")
        elif weight <= 65:
            weight_notes.append(f"Light rider ({weight}kg): Can go 5-15L smaller")
        
        # Skill-based details
        skill_notes = []
        if skill in ("beginner", "novice"):
            skill_notes.append("Beginner: Choose wider, more stable board")
            skill_notes.append("Look for boards with good early planing characteristics")
        elif skill == "advanced":
            skill_notes.append("Advanced: Can handle smaller, more responsive boards")
        
        # Condition-specific advice
        condition_notes = []
        if wind_knots < 10:
            condition_notes.append("Light wind: Larger board helps with early planing")
        elif wind_knots > 20:
            condition_notes.append("Strong wind: Smaller board for better control")
        if weather.wave_height > 1.0:
            condition_notes.append("Waves: Consider board with good wave riding characteristics")
        
        return {
            "size": size,
            "description": description,
            "details": details,
            "weight_notes": weight_notes,
            "skill_notes": skill_notes,
            "condition_notes": condition_notes
        }
    
    def _get_session_advice(self, weather: WeatherConditions, gust_factor: float) -> List[str]:
        """Get session-specific advice"""
        advice = []
        
        # Wind conditions
        if weather.wind_speed_knots < 8:
            advice.append("Wind too light for foiling: wait for better conditions")
        elif weather.wind_speed_knots > 30:
            advice.append("Very strong wind: consider postponing session")
        
        # Gustiness
        if gust_factor > 1.4:
            advice.append("Gusty conditions: practice power management")
        elif gust_factor > 1.2:
            advice.append("Moderately gusty: be prepared for power changes")
        
        # Wave conditions
        if weather.wave_height > 1.5:
            advice.append("Large waves: practice wave riding skills")
        elif weather.wave_height > 0.5:
            advice.append("Waves present: good for wave riding practice")
        else:
            advice.append("Flat water: perfect for learning and freestyle")
        
        # Temperature
        if weather.temperature < 10:
            advice.append("Cold conditions: consider shorter sessions")
        elif weather.temperature < 15:
            advice.append("Cool conditions: dress appropriately")
        
        # Visibility
        if weather.visibility < 5000:
            advice.append("Poor visibility: stay near launch area")
        
        return advice[:4]  # Limit to most important
    
    def _get_general_advice(self, weather: WeatherConditions, gust_factor: float) -> List[str]:
        """Get general wingfoil advice"""
        advice = []
        
        # Safety advice
        if weather.wind_speed_knots > 25:
            advice.append("Strong wind: stay close to shore, use impact vest")
        if weather.uv_index > 6:
            advice.append("High UV: use sun protection")
        
        # Technique advice
        if weather.wind_speed_knots < 12:
            advice.append("Light wind: focus on efficient pumping and early planing")
        elif weather.wind_speed_knots > 20:
            advice.append("Strong wind: prioritize safety and control over speed")
        
        # Equipment advice
        if gust_factor > 1.3:
            advice.append("Gusty conditions: use smaller wing for better control")
        
        # Session planning
        if weather.wave_height > 1.0:
            advice.append("Waves present: great for wave riding and jumping practice")
        else:
            advice.append("Flat water: ideal for learning and freestyle tricks")
        
        return advice[:3]  # Limit to most important
        
    def _generate_conditions_summary(self, weather: WeatherConditions) -> str:
        """Generate a concise summary of conditions for the session"""
        wind_desc = "light" if weather.wind_speed_knots < 12 else \
                   "moderate" if weather.wind_speed_knots < 18 else \
                   "strong" if weather.wind_speed_knots < 25 else \
                   "very strong"
        
        wave_desc = "flat" if weather.wave_height < 0.3 else \
                   "small waves" if weather.wave_height < 1.0 else \
                   "moderate waves" if weather.wave_height < 1.5 else \
                   "large waves"
        
        temp_desc = "cold" if weather.temperature < 12 else \
                   "cool" if weather.temperature < 18 else \
                   "mild" if weather.temperature < 24 else \
                   "warm"
        
        return f"{wind_desc.title()} wind, {wave_desc}, {temp_desc} conditions"

# Global services
weather_service = None
wingfoil_analyzer = None
wingfoil_advisor = None

def load_config():
    """Load configuration from file"""
    config_path = '/app/config/config.json'
    default_config = {
        "location": {
            "name": "Default Location",
            "latitude": 52.5200,  # Berlin as default
            "longitude": 13.4050,
            "shore_direction": 90
        },
        "wingfoil_preferences": {
            "min_wind_knots": 12,
            "max_wind_knots": 30,
            "optimal_wind_min": 15,
            "optimal_wind_max": 25,
            "max_wave_height": 1.5
        },
        "scoring_algorithm": {
            "version": "v2",
            "pillar_weights": {
                "wind": 0.60,
                "surface": 0.20,
                "safety": 0.15,
                "comfort": 0.05
            },
            "skill_targets": {
                "beginner": {
                    "min_wind_ms": 6,
                    "opt_min_wind_ms": 9,
                    "opt_max_wind_ms": 13,
                    "max_wind_ms": 16
                },
                "intermediate": {
                    "min_wind_ms": 7,
                    "opt_min_wind_ms": 10,
                    "opt_max_wind_ms": 15,
                    "max_wind_ms": 18
                },
                "advanced": {
                    "min_wind_ms": 8,
                    "opt_min_wind_ms": 11,
                    "opt_max_wind_ms": 18,
                    "max_wind_ms": 22
                }
            },
            "scoring_parameters": {
                "wind_consistency_penalty": 8,
                "surface_flatwater_penalty": 2.0,
                "surface_height_penalty": 3.0,
                "surface_chop_penalty": 1.5,
                "safety_gust_penalty": 8
            }
        },
        "display_settings": {
            "map_type": "interactive"
        },
        "update_interval_minutes": 30
    }
    
    try:
        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                config = json.load(f)
                # Merge with defaults
                for key, value in default_config.items():
                    if key not in config:
                        config[key] = value
                return config
        else:
            return default_config
    except Exception as e:
        logger.error(f"Error loading config: {e}")
        return default_config

def _sanitize_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Return a redacted version of the config that is safe to return to clients."""
    try:
        import copy
        safe = copy.deepcopy(cfg or {})
        # Redact known integration keys
        integrations = safe.get('integrations') or {}
        for k, v in list(integrations.items()):
            if isinstance(v, str) and v:
                integrations[k] = 'REDACTED'
        safe['integrations'] = integrations
        # Redact any admin token if present
        api_settings = safe.get('api_settings') or {}
        if 'admin_token' in api_settings:
            api_settings['admin_token'] = 'REDACTED'
        safe['api_settings'] = api_settings
        return safe
    except Exception:
        return {}

def _require_admin(request_obj) -> bool:
    """Optional admin guard for mutating endpoints.
    If an admin token is configured (env API_ADMIN_TOKEN or config.api_settings.admin_token),
    the request must include header X-Admin-Token with a matching value.
    If no token is configured, allow (assumes upstream auth/proxy).
    """
    try:
        token = os.getenv('API_ADMIN_TOKEN')
        if not token:
            cfg = load_config()
            token = (cfg.get('api_settings') or {}).get('admin_token')
        if not token:
            return True  # no token configured; rely on upstream protection
        provided = request_obj.headers.get('X-Admin-Token')
        import hmac
        return provided is not None and hmac.compare_digest(str(provided), str(token))
    except Exception:
        return False

def init_services():
    """Initialize global services"""
    global weather_service, wingfoil_analyzer, wingfoil_advisor
    
    config = load_config()
    weather_service = WeatherService(config)
    wingfoil_analyzer = WingfoilAnalyzerV2(config['wingfoil_preferences'], config)
    wingfoil_advisor = WingfoilAdvisor(config.get('wingfoil_preferences', {}), config.get('user', {}))

@app.route('/')
def index():
    """Main dashboard page"""
    return render_template('dashboard.html')

@app.route('/api/current-conditions')
def get_current_conditions():
    """API endpoint for current weather and wingsurf conditions"""
    try:
        # Ensure services are initialized
        if weather_service is None or wingfoil_analyzer is None or wingfoil_advisor is None:
            logger.warning("Services not initialized, initializing now")
            init_services()

        config = load_config()
        location = config['location']

        # Fetch weather data with intelligent caching and model prioritization
        try:
            marine_data = weather_service.fetch_marine_weather(
                location['latitude'], location['longitude']
            )

            # Use high-resolution models first, fallback to consensus if needed
            models_to_try = config.get('models', ['knmi_harmonie_arome_nl', 'dwd_icon_d2'])
            model_results = weather_service.fetch_standard_weather_models(
                location['latitude'], location['longitude'], models_to_try
            )

            if model_results:
                # Use consensus data from multiple models
                standard_data = weather_service._get_consensus_data(model_results)
                logger.info(f"Using consensus data from {len(model_results)} models")
            else:
                # Fallback to single high-resolution model
                standard_data = weather_service.fetch_standard_weather(
                    location['latitude'], location['longitude'], model='knmi_harmonie_arome_nl'
                )
            
            # Validate that we have usable data
            if not marine_data or not marine_data.get('hourly'):
                logger.warning("Invalid marine data received, using fallback")
                marine_data = weather_service._get_fallback_marine_data()
                
            if not standard_data or not standard_data.get('hourly'):
                logger.warning("Invalid standard data received, using fallback")
                standard_data = weather_service._get_fallback_standard_data()
                
        except Exception as e:
            logger.error(f"Critical error fetching weather data: {e}")
            return jsonify({
                "error": "Weather service unavailable", 
                "details": "Using fallback data",
                "fallback": True
            }), 503
        
        # Extract current conditions (first hour of forecast)
        current_time = datetime.now()
        
        # Get arrays
        hourly_standard = standard_data.get('hourly', {})
        hourly_marine = marine_data.get('hourly', {})

        # Determine best index for "now" in the provider's local timezone
        tz_offset_sec = int(standard_data.get('utc_offset_seconds') or 0)
        now_provider = datetime.utcnow().replace(tzinfo=timezone.utc) + timedelta(seconds=tz_offset_sec)

        def nearest_index(times: List[str]) -> int:
            if not times:
                return 0
            try:
                best_i, best_delta = 0, 10**9
                for i, t in enumerate(times):
                    dt = dateparser.isoparse(t)
                    # If times are naive, assume provider's local
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=None)
                    delta = abs((dt - now_provider.replace(tzinfo=None)).total_seconds())
                    if delta < best_delta:
                        best_delta, best_i = delta, i
                return best_i
            except Exception:
                return 0

        std_times: List[str] = hourly_standard.get('time') or []
        mar_times: List[str] = hourly_marine.get('time') or []
        idx_std = nearest_index(std_times)
        idx_mar = nearest_index(mar_times)
        
        # Helper functions to sanitize upstream values
        def safe_get_first(data_dict, key, default=0):
            values = data_dict.get(key)
            if not values or len(values) == 0 or values[0] is None:
                return default
            return values[0]

        def as_float(value, default=0.0):
            try:
                if value is None:
                    return default
                return float(value)
            except Exception:
                return default

        def as_int(value, default=0):
            try:
                if value is None:
                    return default
                return int(round(float(value)))
            except Exception:
                return default
        
        # Use `current` block if present (more accurate), else nearest hourly index
        current_block = standard_data.get('current') or {}
        wind_speed_ms = as_float(current_block.get('wind_speed_10m'), None)
        if wind_speed_ms is None:
            wind_speed_ms = as_float((hourly_standard.get('wind_speed_10m') or [0])[idx_std], 0.0)
        wind_direction = as_int(current_block.get('wind_direction_10m'), None)
        if wind_direction is None:
            wind_direction = as_int((hourly_standard.get('wind_direction_10m') or [0])[idx_std], 0)
        temperature = as_float(current_block.get('temperature_2m'), None)
        if temperature is None:
            temperature = as_float((hourly_standard.get('temperature_2m') or [15])[idx_std], 15.0)
        uv_index_val = as_float(current_block.get('uv_index'), None)
        if uv_index_val is None:
            uv_index_val = as_float((hourly_standard.get('uv_index') or [0])[idx_std], 0.0)

        wave_height = as_float((hourly_marine.get('wave_height') or [0.5])[idx_mar], 0.5)
        wave_period = as_float((hourly_marine.get('wave_period') or [5.0])[idx_mar], 5.0)
        wind_wave_h = as_float((hourly_marine.get('wind_wave_height') or [0.0])[idx_mar], 0.0)
        swell_wave_h = as_float((hourly_marine.get('swell_wave_height') or [0.0])[idx_mar], 0.0)
        wind_wave_p = as_float((hourly_marine.get('wind_wave_period') or [0.0])[idx_mar], 0.0)
        swell_wave_p = as_float((hourly_marine.get('swell_wave_period') or [0.0])[idx_mar], 0.0)
        
        weather_conditions = WeatherConditions(
            timestamp=current_time.isoformat(),
            location=location['name'],
            latitude=location['latitude'],
            longitude=location['longitude'],
            wind_speed_ms=wind_speed_ms,
            wind_speed_knots=wind_speed_ms * 1.944,  # m/s to knots
            wind_direction=wind_direction,
            wind_gust_ms=as_float(safe_get_first(hourly_standard, 'wind_gusts_10m', wind_speed_ms), wind_speed_ms),
            temperature=temperature,
            water_temperature=weather_service.fetch_water_temperature(
                location['latitude'], location['longitude']
            ) or 15.0,
            wave_height=wave_height,
            wave_period=wave_period,
            wave_direction=as_int(safe_get_first(hourly_marine, 'wave_direction', 180), 180),
            pressure=as_float(safe_get_first(hourly_standard, 'pressure_msl', 1013), 1013.0),
            humidity=as_int(safe_get_first(hourly_standard, 'relative_humidity_2m', 50), 50),
            visibility=as_float(safe_get_first(hourly_standard, 'visibility', 10000), 10000.0),
            uv_index=uv_index_val,
            precipitation_mm=as_float(safe_get_first(hourly_standard, 'precipitation', 0.0), 0.0),
            # Derived sport metrics
            shore_angle_deg=as_int(abs(wind_direction - int(location.get('shore_direction', 180))) % 360, 0),
            chop_index=as_float(((wind_wave_h + 0.01) / (swell_wave_h + 0.01)), 0.0)
        )

        # Multi-model consensus (best-effort; API may ignore models param)
        models_to_try = config.get('models', ['knmi_harmonie_arome_nl', 'dwd_icon_d2'])
        model_results = weather_service.fetch_standard_weather_models(location['latitude'], location['longitude'], models_to_try)

        # Optional: OpenWeather current wind for cross-check
        openweather_key = load_config().get('integrations', {}).get('openweather_api_key')
        ow = weather_service.fetch_openweather(location['latitude'], location['longitude'], openweather_key)
        if ow:
            try:
                ow_speed_ms = float(ow['wind']['speed'])
                ow_gust_ms = float(ow['wind'].get('gust', ow_speed_ms))
                model_results['openweather'] = {
                    'hourly': {},
                    'current': {
                        'wind_speed_10m': ow_speed_ms,
                        'wind_gusts_10m': ow_gust_ms
                    }
                }
            except Exception:
                pass

        # Enhanced data averaging between OpenWeather and Open-Meteo
        def average_values(open_meteo_val: float, openweather_val: Optional[float]) -> float:
            """Average values from Open-Meteo and OpenWeather equally, fallback to available source."""
            if openweather_val is not None and open_meteo_val is not None:
                return (open_meteo_val + openweather_val) / 2.0
            return open_meteo_val if open_meteo_val is not None else (openweather_val or 0.0)
            
        # Enhanced wind data with OpenWeather averaging
        enhanced_wind_speed_ms = wind_speed_ms
        enhanced_gust_ms = weather_conditions.wind_gust_ms
        
        if ow:
            try:
                ow_speed_ms = float(ow['wind']['speed'])
                ow_gust_ms = float(ow['wind'].get('gust', ow_speed_ms))
                enhanced_wind_speed_ms = average_values(wind_speed_ms, ow_speed_ms)
                enhanced_gust_ms = average_values(weather_conditions.wind_gust_ms, ow_gust_ms)
                logger.info(f"Averaged wind data: Open-Meteo {wind_speed_ms:.1f}m/s, OpenWeather {ow_speed_ms:.1f}m/s, Result {enhanced_wind_speed_ms:.1f}m/s")
            except Exception as e:
                logger.warning(f"Error processing OpenWeather data for averaging: {e}")
        
        # Update weather conditions with enhanced values
        weather_conditions.wind_speed_ms = enhanced_wind_speed_ms
        weather_conditions.wind_speed_knots = enhanced_wind_speed_ms * 1.944
        weather_conditions.wind_gust_ms = enhanced_gust_ms
        
        # Update shore angle calculation with any potential wind direction changes
        weather_conditions.shore_angle_deg = as_int(abs(wind_direction - int(location.get('shore_direction', 90))) % 360, 0)
        
        # Now analyze wingfoil conditions with enhanced wind data using v2 algorithm
        wingfoil_conditions_v2 = wingfoil_analyzer.analyze_conditions_v2(
            weather_conditions, 
            int(location.get('shore_direction', 90)),
            model_results.get('consensus', {})
        )
        wingfoil_advice = wingfoil_advisor.compute_advice(weather_conditions)

        def collect_model_value(model_data: Dict[str, Any], key: str, default: float = 0.0) -> float:
            hourly = (model_data or {}).get('hourly', {})
            v = safe_get_first(hourly, key, default)
            return as_float(v, default)

        values_speed, values_gust = [], []
        weights_speed, weights_gust = [], []
        per_model: Dict[str, Any] = {}
        # Optional model weights from config
        model_weights: Dict[str, float] = (config.get('model_weights') or {})
        for model_name, payload in model_results.items():
            sp = collect_model_value(payload, 'wind_speed_10m', wind_speed_ms)
            gu = collect_model_value(payload, 'wind_gusts_10m', weather_conditions.wind_gust_ms)
            per_model[model_name] = {
                'wind_speed_knots': round(sp * 1.944, 1),
                'wind_gust_knots': round(gu * 1.944, 1),
            }
            values_speed.append(sp)
            values_gust.append(gu)
            # Default weight 1.0 if not configured
            w = float(model_weights.get(model_name, 1.0))
            weights_speed.append(w)
            weights_gust.append(w)

        def median(lst: List[float]) -> float:
            s = sorted(lst)
            if not s:
                return 0.0
            n = len(s)
            return (s[n//2] if n % 2 == 1 else (s[n//2-1] + s[n//2]) / 2)

        def weighted_mean(values: List[float], weights: List[float]) -> float:
            if not values:
                return 0.0
            if not weights or len(weights) != len(values):
                weights = [1.0] * len(values)
            total_w = sum(weights)
            if total_w <= 0:
                weights = [1.0] * len(values)
                total_w = float(len(values))
            return sum(v * w for v, w in zip(values, weights)) / total_w

        # Compute consensus and derived gust factor
        consensus = {
            'models_used': list(per_model.keys()),
            'weights_used': {k: float(model_weights.get(k, 1.0)) for k in per_model.keys()},
            'median_wind_knots': round(median(values_speed) * 1.944, 1) if values_speed else round(weather_conditions.wind_speed_knots, 1),
            'median_gust_knots': round(median(values_gust) * 1.944, 1) if values_gust else round(float(weather_conditions.wind_gust_ms) * 1.944, 1),
            'weighted_wind_knots': round(weighted_mean(values_speed, weights_speed) * 1.944, 1) if values_speed else round(weather_conditions.wind_speed_knots, 1),
            'weighted_gust_knots': round(weighted_mean(values_gust, weights_gust) * 1.944, 1) if values_gust else round(float(weather_conditions.wind_gust_ms) * 1.944, 1),
            'spread_knots': round((max(values_speed) - min(values_speed)) * 1.944, 1) if len(values_speed) >= 2 else 0.0
        }
        try:
            m_wind = max(consensus['median_wind_knots'], 0.1)
            consensus['gust_factor'] = round(consensus['median_gust_knots'] / m_wind, 2)
        except Exception:
            consensus['gust_factor'] = 1.0
        # Attach representative valid time (nearest hourly from Open‑Meteo) and generation timestamp
        try:
            consensus['valid_time_iso'] = (std_times[idx_std] if (std_times and 0 <= idx_std < len(std_times)) else weather_conditions.timestamp)
        except Exception:
            consensus['valid_time_iso'] = weather_conditions.timestamp
        consensus['generated_at_iso'] = datetime.now().isoformat()
        
        # UI/display settings to help client render overlays
        ui_settings = {
            "map_type": config.get('display_settings', {}).get('map_type', 'interactive')
        }

        return jsonify({
            "weather": asdict(weather_conditions),
            "wingfoil": {
                "score": wingfoil_conditions_v2['overall_score'],
                "suitable": wingfoil_conditions_v2['suitable'],
                "overall_conditions": wingfoil_conditions_v2['conditions'],
                "confidence": wingfoil_conditions_v2['confidence'],
                "pillars": wingfoil_conditions_v2['pillars'],
                "weights": wingfoil_conditions_v2['weights'],
                "weighted_contributions": wingfoil_conditions_v2['weighted_contributions'],
                "skill_level": wingfoil_conditions_v2['skill_level'],
                "score_breakdown": wingfoil_conditions_v2['score_breakdown'],
                "computation_trace": wingfoil_conditions_v2['computation_trace'],
                "wind_evaluation": f"Wind: {wingfoil_conditions_v2['pillars']['wind']['wind_speed_ms']:.1f}m/s, gust factor {wingfoil_conditions_v2['pillars']['wind']['gust_factor']:.2f}",
                "wave_evaluation": f"Waves: {wingfoil_conditions_v2['pillars']['surface']['wave_height']:.1f}m, period {wingfoil_conditions_v2['pillars']['surface']['wave_period']:.1f}s",
                "recommendations": wingfoil_advice.get('combined_advice', [])[:4]
            },
            "wingfoil_advice": wingfoil_advice,
            "display_settings": ui_settings,
            "sport_metrics": {
                "shore_angle_deg": weather_conditions.shore_angle_deg,
                "shore_direction_deg": int(location.get('shore_direction', 90)),
                "wind_to_shore_angle_deg": weather_conditions.shore_angle_deg,
                "chop_index": round(weather_conditions.chop_index, 2),
                "wind_wave_height": wind_wave_h,
                "swell_wave_height": swell_wave_h,
                "wind_wave_period": wind_wave_p,
                "swell_wave_period": swell_wave_p
            },
            "multi_model": {
                "per_model": per_model,
                "consensus": consensus
            }
        })
        
    except Exception as e:
        logger.error(f"Error getting current conditions: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/inkypi/morning-report')
def get_inkypi_morning_report():
    """
    Special endpoint for InkyPi morning reports
    
    Returns simplified, formatted data optimized for e-ink display
    """
    try:
        # Use daily summary for the day plan + current for snapshot
        daily_res = get_daily_summary()
        if daily_res.status_code != 200:
            return daily_res
        daily = daily_res.get_json()

        current_res = get_current_conditions()
        if current_res.status_code != 200:
            return current_res
        current = current_res.get_json()
        weather = current['weather']
        wingfoil = current['wingfoil']
        wingfoil_advice = current.get('wingfoil_advice', {})
        
        # Safe formatting helpers
        def fmt_num(value, unit="", digits=1):
            try:
                return f"{float(value):.{digits}f}{unit}"
            except Exception:
                return "N/A"

        # Format for InkyPi display (robust to missing values)
        morning_report = {
            "title": "Morning Wingfoil Report",
            "location": weather.get('location', 'Unknown'),
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "conditions": {
                "wind": f"{fmt_num(weather.get('wind_speed_knots'), ' knots')} @ {weather.get('wind_direction', '—')}°",
                "waves": f"{fmt_num(weather.get('wave_height'), 'm')} / {fmt_num(weather.get('wave_period'), 's')}",
                "air_temp": f"{fmt_num(weather.get('temperature'), '°C')}",
                "water_temp": f"{fmt_num(weather.get('water_temperature'), '°C')}",
                "pressure": f"{fmt_num(weather.get('pressure'), ' hPa', digits=0)}"
            },
            "wingfoil_assessment": {
                "suitable": bool(wingfoil.get('suitable', False)),
                "score": int(wingfoil.get('score', 0)),
                "condition": wingfoil.get('overall_conditions', 'Unknown'),
                "wind_eval": wingfoil.get('wind_evaluation', 'N/A'),
                "wave_eval": wingfoil.get('wave_evaluation', 'N/A')
            },
            "wingfoil_advice": {
                "recommended_wing_size": wingfoil_advice.get('recommended_wing_size', '—'),
                "gust_factor": wingfoil_advice.get('gust_factor', '—')
            },
            "day_plan": {
                "day": daily.get('day'),
                "wind": daily.get('wind_knots'),
                "gust": daily.get('gust_knots'),
                "temp": daily.get('temperature_c'),
                "waves": daily.get('wave_height_m'),
                "optimal_windows": daily.get('optimal_windows', [])
            },
            "recommendations": (wingfoil.get('recommendations') or [])[:3],
            "summary": f"Wingfoil conditions: {wingfoil.get('overall_conditions', 'Unknown')} ({wingfoil.get('score', 0)}/100)"
        }
        
        return jsonify(morning_report)
        
    except Exception as e:
        logger.error(f"Error generating morning report: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/forecast/<int:hours>')
def get_forecast(hours: int):
    """Get forecast for next N hours"""
    # Note: Detailed forecast analysis can be implemented using the v2 scoring system
    return jsonify({"message": f"Forecast for next {hours} hours - coming soon!"})

@app.route('/api/5day-forecast')
def get_5day_forecast():
    """Provide a 5-day daily overview blending Open‑Meteo and OpenWeather equally.
    For each day: avg wind, max gust, avg temperature, avg wave height, simple wingfoil suitability.
    """
    try:
        config = load_config()
        location = config['location']
        lat = location['latitude']
        lon = location['longitude']

        # Use high-resolution models for 5-day forecast
        models_to_try = ['knmi_harmonie_arome_nl', 'dwd_icon_d2']
        model_results = weather_service.fetch_standard_weather_models(lat, lon, models_to_try)

        if model_results:
            # Use the highest resolution model available
            best_model = 'knmi_harmonie_arome_nl' if 'knmi_harmonie_arome_nl' in model_results else 'dwd_icon_d2'
            std = model_results[best_model]
            logger.info(f"Using high-resolution model {best_model} for 5-day forecast")
        else:
            # Fallback to standard method
            std = weather_service.fetch_standard_weather(lat, lon, model='knmi_harmonie_arome_nl')

        mar = weather_service.fetch_marine_weather(lat, lon)
        ow_key = (config.get('integrations') or {}).get('openweather_api_key')
        ow_fc = weather_service.fetch_openweather_forecast(lat, lon, ow_key)

        tz_offset_sec = int(std.get('utc_offset_seconds') or 0)
        # Build per-day buckets
        from collections import defaultdict
        days: Dict[str, Dict[str, List[float]]] = defaultdict(lambda: defaultdict(list))

        # Open‑Meteo hourly
        h = std.get('hourly', {})
        times = h.get('time') or []
        for i, t in enumerate(times):
            try:
                dt = dateparser.isoparse(t)
                day_key = str((dt + timedelta(seconds=0)).date())
            except Exception:
                continue
            def get(arr, idx, default=0.0):
                a = h.get(arr) or []
                try:
                    v = float(a[idx]) if idx < len(a) and a[idx] is not None else default
                except Exception:
                    v = default
                return v
            wind_ms = get('wind_speed_10m', i, 0.0)
            gust_ms = get('wind_gusts_10m', i, wind_ms)
            temp_c = get('temperature_2m', i, 0.0)
            days[day_key]['om_wind_ms'].append(wind_ms)
            days[day_key]['om_gust_ms'].append(gust_ms)
            days[day_key]['om_temp_c'].append(temp_c)

        # Open‑Meteo Marine hourly (for waves)
        mh = mar.get('hourly', {})
        mtimes = mh.get('time') or []
        for i, t in enumerate(mtimes):
            try:
                dt = dateparser.isoparse(t)
                day_key = str(dt.date())
            except Exception:
                continue
            try:
                wave_h = float((mh.get('wave_height') or [None])[i] or 0.0)
            except Exception:
                wave_h = 0.0
            days[day_key]['om_wave_m'].append(wave_h)

        # OpenWeather 3-hour list
        if ow_fc and isinstance(ow_fc.get('list'), list):
            for item in ow_fc['list']:
                try:
                    t = item.get('dt_txt') or ''
                    dt = dateparser.isoparse(t)
                    day_key = str(dt.date())
                    wind_ms = float(item.get('wind', {}).get('speed', 0.0))
                    gust_ms = float(item.get('wind', {}).get('gust', wind_ms))
                    temp_c = float(item.get('main', {}).get('temp', 0.0))
                except Exception:
                    continue
                days[day_key]['ow_wind_ms'].append(wind_ms)
                days[day_key]['ow_gust_ms'].append(gust_ms)
                days[day_key]['ow_temp_c'].append(temp_c)

        # Build 5 sequential days from local now
        local_now = datetime.utcnow() + timedelta(seconds=tz_offset_sec)
        result = []
        for d in range(0, 5):
            day_key = str((local_now + timedelta(days=d)).date())
            bucket = days.get(day_key, {})
            def avg(vals: List[float]) -> float:
                vals = [v for v in (vals or []) if v is not None]
                return round(sum(vals) / len(vals), 2) if vals else 0.0
            def mx(vals: List[float]) -> float:
                vals = [v for v in (vals or []) if v is not None]
                return round(max(vals), 2) if vals else 0.0
            # Equal blend where both present; else fall back to present source
            om_wind = avg(bucket.get('om_wind_ms'))
            ow_wind = avg(bucket.get('ow_wind_ms'))
            wind_ms = round(((om_wind + ow_wind) / 2.0) if (om_wind and ow_wind) else (om_wind or ow_wind), 2)
            om_gust = avg(bucket.get('om_gust_ms'))
            ow_gust = avg(bucket.get('ow_gust_ms'))
            gust_ms = round(((om_gust + ow_gust) / 2.0) if (om_gust and ow_gust) else (om_gust or ow_gust), 2)
            om_temp = avg(bucket.get('om_temp_c'))
            ow_temp = avg(bucket.get('ow_temp_c'))
            temp_c = round(((om_temp + ow_temp) / 2.0) if (om_temp and ow_temp) else (om_temp or ow_temp), 1)
            wave_m = avg(bucket.get('om_wave_m'))

            wind_knots = round(wind_ms * 1.944, 1)
            gust_knots = round(gust_ms * 1.944, 1)

            # Simple suitability scoring for day overview
            prefs = config.get('wingfoil_preferences', {})
            min_w = float(prefs.get('min_wind_knots', 12))
            max_w = float(prefs.get('max_wind_knots', 30))
            opt_min = float(prefs.get('optimal_wind_min', 15))
            opt_max = float(prefs.get('optimal_wind_max', 25))
            if wind_knots < min_w:
                suitability = "Too light"
            elif wind_knots > max_w:
                suitability = "Too strong"
            elif opt_min <= wind_knots <= opt_max:
                suitability = "Optimal"
            else:
                suitability = "Usable"

            result.append({
                "day": day_key,
                "wind_knots_avg": wind_knots,
                "gust_knots_avg": gust_knots,
                "temperature_c_avg": temp_c,
                "wave_height_m_avg": round(wave_m, 2),
                "suitability": suitability
            })

        return jsonify({
            "location": location['name'],
            "days": result
        })
    except Exception as e:
        logger.error(f"Error building 5-day forecast: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/hourly-forecast')
def get_hourly_forecast():
    """Get hourly forecast for the current day using high-resolution models"""
    try:
        config = load_config()
        location = config['location']

        # Fetch weather data with intelligent caching and high-resolution models
        try:
            marine_data = weather_service.fetch_marine_weather(
                location['latitude'], location['longitude']
            )

            # Prioritize high-resolution models for hourly forecasts
            models_to_try = ['knmi_harmonie_arome_nl', 'dwd_icon_d2']
            model_results = weather_service.fetch_standard_weather_models(
                location['latitude'], location['longitude'], models_to_try
            )

            if model_results:
                # Use the highest resolution model available
                best_model = 'knmi_harmonie_arome_nl' if 'knmi_harmonie_arome_nl' in model_results else 'dwd_icon_d2'
                standard_data = model_results[best_model]
                logger.info(f"Using high-resolution model {best_model} for hourly forecast")
            else:
                # Fallback to standard method
                standard_data = weather_service.fetch_standard_weather(
                    location['latitude'], location['longitude'], model='knmi_harmonie_arome_nl'
                )
            
            # Validate that we have usable data
            if not marine_data or not marine_data.get('hourly'):
                logger.warning("Invalid marine data received for hourly forecast, using fallback")
                marine_data = weather_service._get_fallback_marine_data()
                
            if not standard_data or not standard_data.get('hourly'):
                logger.warning("Invalid standard data received for hourly forecast, using fallback")
                standard_data = weather_service._get_fallback_standard_data()
                
        except Exception as e:
            logger.error(f"Critical error fetching weather data for hourly forecast: {e}")
            return jsonify({
                "error": "Weather service unavailable", 
                "details": "Using fallback data",
                "fallback": True
            }), 503
        
        # Get timezone info
        tz_offset_sec = int(standard_data.get('utc_offset_seconds') or 0)
        local_now = datetime.utcnow() + timedelta(seconds=tz_offset_sec)
        local_day = local_now.date()
        
        # Get hourly data
        hourly_standard = standard_data.get('hourly', {})
        hourly_marine = marine_data.get('hourly', {})
        
        times: List[str] = hourly_standard.get('time') or []
        
        # Filter for today's hours only (excluding night hours 22:00-04:00)
        today_indices = []
        today_times = []
        for i, time_str in enumerate(times):
            try:
                dt = dateparser.isoparse(time_str)
                if dt.date() == local_day:
                    hour = dt.hour
                    # Exclude night hours (22:00-04:00)
                    if not (hour >= 22 or hour <= 4):
                        today_indices.append(i)
                        today_times.append(time_str)
            except Exception:
                continue
        
        # Helper functions
        def safe_get_hourly(data_dict, key: str, indices: List[int], default=0):
            values = data_dict.get(key, [])
            result = []
            for i in indices:
                if i < len(values) and values[i] is not None:
                    try:
                        result.append(float(values[i]))
                    except (ValueError, TypeError):
                        result.append(default)
                else:
                    result.append(default)
            return result
        
        # Get hourly values for today
        wind_speeds_ms = safe_get_hourly(hourly_standard, 'wind_speed_10m', today_indices, 0.0)
        wind_directions = safe_get_hourly(hourly_standard, 'wind_direction_10m', today_indices, 180)
        wind_gusts_ms = safe_get_hourly(hourly_standard, 'wind_gusts_10m', today_indices, 0.0)
        temperatures = safe_get_hourly(hourly_standard, 'temperature_2m', today_indices, 20.0)
        pressures = safe_get_hourly(hourly_standard, 'pressure_msl', today_indices, 1013.0)
        humidity = safe_get_hourly(hourly_standard, 'relative_humidity_2m', today_indices, 60)
        uv_indices = safe_get_hourly(hourly_standard, 'uv_index', today_indices, 0.0)
        
        # Marine data (may have different time intervals)
        marine_times = hourly_marine.get('time', [])
        marine_today_indices = []
        for i, time_str in enumerate(marine_times):
            try:
                dt = dateparser.isoparse(time_str)
                if dt.date() == local_day:
                    marine_today_indices.append(i)
            except Exception:
                continue
        
        wave_heights = safe_get_hourly(hourly_marine, 'wave_height', marine_today_indices, 0.5)
        wave_periods = safe_get_hourly(hourly_marine, 'wave_period', marine_today_indices, 5.0)
        wave_directions = safe_get_hourly(hourly_marine, 'wave_direction', marine_today_indices, 180)
        
        # Create hourly forecast data
        hourly_forecast = []
        
        for i, time_str in enumerate(today_times):
            try:
                dt = dateparser.isoparse(time_str)
                hour_display = dt.strftime("%H:%M")
                
                # Get values for this hour
                wind_speed_ms = wind_speeds_ms[i] if i < len(wind_speeds_ms) else 0.0
                wind_speed_knots = wind_speed_ms * 1.944
                wind_dir = int(wind_directions[i]) if i < len(wind_directions) else 180
                wind_gust_ms = wind_gusts_ms[i] if i < len(wind_gusts_ms) else wind_speed_ms
                temp = temperatures[i] if i < len(temperatures) else 20.0
                
                # Marine data (interpolate if needed since marine data might be less frequent)
                marine_index = min(i, len(wave_heights) - 1) if wave_heights else 0
                wave_height = wave_heights[marine_index] if wave_heights else 0.5
                wave_period = wave_periods[marine_index] if wave_periods else 5.0
                
                # Create weather conditions for this hour
                hour_conditions = WeatherConditions(
                    timestamp=time_str,
                    location=location['name'],
                    latitude=location['latitude'],
                    longitude=location['longitude'],
                    wind_speed_ms=wind_speed_ms,
                    wind_speed_knots=wind_speed_knots,
                    wind_direction=wind_dir,
                    wind_gust_ms=wind_gust_ms,
                    temperature=temp,
                    water_temperature=15.0,  # Use default for hourly
                    wave_height=wave_height,
                    wave_period=wave_period,
                    wave_direction=180,  # Default
                    pressure=pressures[i] if i < len(pressures) else 1013.0,
                    humidity=int(humidity[i]) if i < len(humidity) else 60,
                    visibility=10000.0,  # Default
                    uv_index=uv_indices[i] if i < len(uv_indices) else 0.0,
                    precipitation_mm=safe_get_hourly(hourly_standard, 'precipitation', today_indices, 0.0)[i] if i < len(today_indices) else 0.0,
                    shore_angle_deg=abs(wind_dir - location.get('shore_direction', 90)) % 360,
                    chop_index=1.0  # Default
                )
                
                # Comprehensive wingfoil analysis for this hour using v2 algorithm
                try:
                    # Use comprehensive scoring system
                    wingfoil_conditions_v2 = wingfoil_analyzer.analyze_conditions_v2(
                        hour_conditions,
                        int(location.get('shore_direction', 90)),
                        {}  # No model consensus for hourly data
                    )

                    # Get wing size recommendation
                    rider_weight = config.get('user', {}).get('rider_weight_kg', 80)

                    # Base wing sizes for ~80kg rider
                    if wind_speed_knots < 8:
                        base_size = "7-8m"
                    elif wind_speed_knots < 12:
                        base_size = "6-7m"
                    elif wind_speed_knots < 16:
                        base_size = "5-6m"
                    elif wind_speed_knots < 20:
                        base_size = "4-5m"
                    elif wind_speed_knots < 25:
                        base_size = "3.5-4m"
                    else:
                        base_size = "3m"

                    # Adjust for rider weight
                    if rider_weight >= 90:
                        if wind_speed_knots < 8:
                            wing_size = "8-9m"
                        elif wind_speed_knots < 12:
                            wing_size = "7-8m"
                        elif wind_speed_knots < 16:
                            wing_size = "6-7m"
                        elif wind_speed_knots < 20:
                            wing_size = "5-6m"
                        elif wind_speed_knots < 25:
                            wing_size = "4-5m"
                        else:
                            wing_size = "3.5-4m"
                    elif rider_weight <= 65:
                        if wind_speed_knots < 8:
                            wing_size = "6-7m"
                        elif wind_speed_knots < 12:
                            wing_size = "5-6m"
                        elif wind_speed_knots < 16:
                            wing_size = "4-5m"
                        elif wind_speed_knots < 20:
                            wing_size = "3.5-4m"
                        elif wind_speed_knots < 25:
                            wing_size = "3m"
                        else:
                            wing_size = "2.5-3m"
                    else:
                        wing_size = base_size

                    wingfoil_data = {
                        "score": wingfoil_conditions_v2['overall_score'],
                        "suitable": wingfoil_conditions_v2['suitable'],
                        "overall_conditions": wingfoil_conditions_v2['conditions'],
                        "wind_evaluation": f"{wingfoil_conditions_v2['pillars']['wind']['speed_score']:.1f} speed, {wingfoil_conditions_v2['pillars']['wind']['direction_score']:.1f} direction",
                        "wing_size": wing_size,
                        "pillars": wingfoil_conditions_v2['pillars'],
                        "confidence": wingfoil_conditions_v2['confidence']
                    }
                except Exception as e:
                    logger.warning(f"Error analyzing wingfoil conditions for hour {hour_display}: {e}")
                    wingfoil_data = {
                        "score": 0,
                        "suitable": False,
                        "overall_conditions": "Analysis Error",
                        "wind_evaluation": "N/A",
                        "wing_size": "N/A",
                        "pillars": None,
                        "confidence": 0.0
                    }
                
                # Create summary for this hour
                hour_summary = {
                    "time": hour_display,
                    "timestamp": time_str,
                    "wind": {
                        "speed_knots": round(wind_speed_knots, 1),
                        "direction": wind_dir,
                        "gust_knots": round(wind_gust_ms * 1.944, 1)
                    },
                    "waves": {
                        "height_m": round(wave_height, 1),
                        "period_s": round(wave_period, 1)
                    },
                    "conditions": {
                        "temperature": round(temp, 1),
                        "uv_index": round(uv_indices[i] if i < len(uv_indices) else 0.0, 1),
                        "pressure": round(pressures[i] if i < len(pressures) else 1013.0, 0)
                    },
                    "wingfoil": wingfoil_data
                }
                
                hourly_forecast.append(hour_summary)
                
            except Exception as e:
                logger.warning(f"Error processing hour {i}: {e}")
                continue
        
        return jsonify({
            "date": str(local_day),
            "location": location['name'],
            "hourly_forecast": hourly_forecast,
            "summary": {
                "total_hours": len(hourly_forecast),
                "good_hours": len([h for h in hourly_forecast if h['wingfoil']['score'] >= 70]),
                "suitable_hours": len([h for h in hourly_forecast if h['wingfoil']['suitable']])
            }
        })
        
    except Exception as e:
        logger.error(f"Error getting hourly forecast: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/tomorrow-forecast')
def get_tomorrow_forecast():
    """Get hourly forecast for tomorrow using high-resolution models"""
    try:
        config = load_config()
        location = config['location']

        # Fetch weather data with high-resolution models
        try:
            marine_data = weather_service.fetch_marine_weather(
                location['latitude'], location['longitude']
            )

            # Use high-resolution models for tomorrow's forecast
            models_to_try = ['knmi_harmonie_arome_nl', 'dwd_icon_d2']
            model_results = weather_service.fetch_standard_weather_models(
                location['latitude'], location['longitude'], models_to_try
            )

            if model_results:
                # Use the highest resolution model available
                best_model = 'knmi_harmonie_arome_nl' if 'knmi_harmonie_arome_nl' in model_results else 'dwd_icon_d2'
                standard_data = model_results[best_model]
                logger.info(f"Using high-resolution model {best_model} for tomorrow forecast")
            else:
                # Fallback to standard method
                standard_data = weather_service.fetch_standard_weather(
                    location['latitude'], location['longitude'], model='knmi_harmonie_arome_nl'
                )
            
            if not marine_data or not marine_data.get('hourly'):
                logger.warning("Invalid marine data received for tomorrow forecast, using fallback")
                marine_data = weather_service._get_fallback_marine_data()
                
            if not standard_data or not standard_data.get('hourly'):
                logger.warning("Invalid standard data received for tomorrow forecast, using fallback")
                standard_data = weather_service._get_fallback_standard_data()
                
        except Exception as e:
            logger.error(f"Critical error fetching weather data for tomorrow forecast: {e}")
            return jsonify({
                "error": "Weather service unavailable", 
                "details": "Using fallback data",
                "fallback": True
            }), 503
        
        # Get timezone info
        tz_offset_sec = int(standard_data.get('utc_offset_seconds') or 0)
        local_now = datetime.utcnow() + timedelta(seconds=tz_offset_sec)
        tomorrow = (local_now + timedelta(days=1)).date()
        
        # Get hourly data
        hourly_standard = standard_data.get('hourly', {})
        hourly_marine = marine_data.get('hourly', {})
        
        times: List[str] = hourly_standard.get('time') or []
        
        # Filter for tomorrow's hours only (excluding night hours 22:00-04:00)
        tomorrow_indices = []
        tomorrow_times = []
        for i, time_str in enumerate(times):
            try:
                dt = dateparser.isoparse(time_str)
                if dt.date() == tomorrow:
                    hour = dt.hour
                    # Exclude night hours (22:00-04:00)
                    if not (hour >= 22 or hour <= 4):
                        tomorrow_indices.append(i)
                        tomorrow_times.append(time_str)
            except Exception:
                continue
        
        # Helper function to get hourly values
        def safe_get_hourly_tomorrow(data_dict, key: str, indices: List[int], default=0):
            values = data_dict.get(key, [])
            result = []
            for i in indices:
                if i < len(values) and values[i] is not None:
                    try:
                        result.append(float(values[i]))
                    except (ValueError, TypeError):
                        result.append(default)
                else:
                    result.append(default)
            return result
        
        # Get hourly values for tomorrow
        wind_speeds_ms = safe_get_hourly_tomorrow(hourly_standard, 'wind_speed_10m', tomorrow_indices, 0.0)
        wind_directions = safe_get_hourly_tomorrow(hourly_standard, 'wind_direction_10m', tomorrow_indices, 180)
        wind_gusts_ms = safe_get_hourly_tomorrow(hourly_standard, 'wind_gusts_10m', tomorrow_indices, 0.0)
        temperatures = safe_get_hourly_tomorrow(hourly_standard, 'temperature_2m', tomorrow_indices, 20.0)
        
        # Marine data for tomorrow
        marine_times = hourly_marine.get('time', [])
        marine_tomorrow_indices = []
        for i, time_str in enumerate(marine_times):
            try:
                dt = dateparser.isoparse(time_str)
                if dt.date() == tomorrow:
                    marine_tomorrow_indices.append(i)
            except Exception:
                continue
        
        wave_heights = safe_get_hourly_tomorrow(hourly_marine, 'wave_height', marine_tomorrow_indices, 0.5)
        wave_periods = safe_get_hourly_tomorrow(hourly_marine, 'wave_period', marine_tomorrow_indices, 5.0)
        
        # Create tomorrow's hourly forecast data
        tomorrow_forecast = []
        
        for i, time_str in enumerate(tomorrow_times):
            try:
                dt = dateparser.isoparse(time_str)
                hour_display = dt.strftime("%H:%M")
                
                # Get values for this hour
                wind_speed_ms = wind_speeds_ms[i] if i < len(wind_speeds_ms) else 0.0
                wind_speed_knots = wind_speed_ms * 1.944
                wind_dir = int(wind_directions[i]) if i < len(wind_directions) else 180
                wind_gust_ms = wind_gusts_ms[i] if i < len(wind_gusts_ms) else wind_speed_ms
                temp = temperatures[i] if i < len(temperatures) else 20.0
                
                # Marine data
                marine_index = min(i, len(wave_heights) - 1) if wave_heights else 0
                wave_height = wave_heights[marine_index] if wave_heights else 0.5
                wave_period = wave_periods[marine_index] if wave_periods else 5.0

                # Create weather conditions for this hour
                tomorrow_conditions = WeatherConditions(
                    timestamp=time_str,
                    location=location['name'],
                    latitude=location['latitude'],
                    longitude=location['longitude'],
                    wind_speed_ms=wind_speed_ms,
                    wind_speed_knots=wind_speed_knots,
                    wind_direction=wind_dir,
                    wind_gust_ms=wind_gust_ms,
                    temperature=temp,
                    water_temperature=15.0,  # Use default for tomorrow
                    wave_height=wave_height,
                    wave_period=wave_period,
                    wave_direction=180,  # Default
                    pressure=1013.0,  # Default
                    humidity=60,  # Default
                    visibility=10000.0,  # Default
                    uv_index=0.0,  # Default
                    precipitation_mm=0.0,
                    shore_angle_deg=abs(wind_dir - location.get('shore_direction', 90)) % 360,
                    chop_index=1.0  # Default
                )

                # Comprehensive wingfoil analysis for this hour using v2 algorithm
                try:
                    # Use comprehensive scoring system
                    wingfoil_conditions_v2 = wingfoil_analyzer.analyze_conditions_v2(
                        tomorrow_conditions,
                        int(location.get('shore_direction', 90)),
                        {}  # No model consensus for tomorrow data
                    )

                    # Get wing size recommendation
                    rider_weight = config.get('user', {}).get('rider_weight_kg', 80)

                    # Base wing sizes for ~80kg rider
                    if wind_speed_knots < 8:
                        base_size = "7-8m"
                    elif wind_speed_knots < 12:
                        base_size = "6-7m"
                    elif wind_speed_knots < 16:
                        base_size = "5-6m"
                    elif wind_speed_knots < 20:
                        base_size = "4-5m"
                    elif wind_speed_knots < 25:
                        base_size = "3.5-4m"
                    else:
                        base_size = "3m"

                    # Adjust for rider weight
                    if rider_weight >= 90:
                        if wind_speed_knots < 8:
                            wing_size = "8-9m"
                        elif wind_speed_knots < 12:
                            wing_size = "7-8m"
                        elif wind_speed_knots < 16:
                            wing_size = "6-7m"
                        elif wind_speed_knots < 20:
                            wing_size = "5-6m"
                        elif wind_speed_knots < 25:
                            wing_size = "4-5m"
                        else:
                            wing_size = "3.5-4m"
                    elif rider_weight <= 65:
                        if wind_speed_knots < 8:
                            wing_size = "6-7m"
                        elif wind_speed_knots < 12:
                            wing_size = "5-6m"
                        elif wind_speed_knots < 16:
                            wing_size = "4-5m"
                        elif wind_speed_knots < 20:
                            wing_size = "3.5-4m"
                        elif wind_speed_knots < 25:
                            wing_size = "3m"
                        else:
                            wing_size = "2.5-3m"
                    else:
                        wing_size = base_size

                    wingfoil_data = {
                        "score": wingfoil_conditions_v2['overall_score'],
                        "suitable": wingfoil_conditions_v2['suitable'],
                        "overall_conditions": wingfoil_conditions_v2['conditions'],
                        "wind_evaluation": f"{wingfoil_conditions_v2['pillars']['wind']['speed_score']:.1f} speed, {wingfoil_conditions_v2['pillars']['wind']['direction_score']:.1f} direction",
                        "wing_size": wing_size,
                        "pillars": wingfoil_conditions_v2['pillars'],
                        "confidence": wingfoil_conditions_v2['confidence']
                    }
                except Exception as e:
                    logger.warning(f"Error analyzing wingfoil conditions for tomorrow hour {hour_display}: {e}")
                    wingfoil_data = {
                        "score": 0,
                        "suitable": False,
                        "overall_conditions": "Analysis Error",
                        "wind_evaluation": "N/A",
                        "wing_size": "N/A",
                        "pillars": None,
                        "confidence": 0.0
                    }
                
                # Create summary for this hour
                hour_summary = {
                    "time": hour_display,
                    "timestamp": time_str,
                    "wind": {
                        "speed_knots": round(wind_speed_knots, 1),
                        "direction": wind_dir,
                        "gust_knots": round(wind_gust_ms * 1.944, 1)
                    },
                    "waves": {
                        "height_m": round(wave_height, 1),
                        "period_s": round(wave_period, 1)
                    },
                    "conditions": {
                        "temperature": round(temp, 1)
                    },
                    "wingfoil": wingfoil_data
                }
                
                tomorrow_forecast.append(hour_summary)
                
            except Exception as e:
                logger.warning(f"Error processing tomorrow hour {i}: {e}")
                continue
        
        return jsonify({
            "date": str(tomorrow),
            "location": location['name'],
            "hourly_forecast": tomorrow_forecast,
            "summary": {
                "total_hours": len(tomorrow_forecast),
                "good_hours": len([h for h in tomorrow_forecast if h['wingfoil']['score'] >= 70]),
                "suitable_hours": len([h for h in tomorrow_forecast if h['wingfoil']['suitable']])
            }
        })
        
    except Exception as e:
        logger.error(f"Error getting tomorrow forecast: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/daily-summary')
def get_daily_summary():
    """Daily summary for the current local day at the configured location"""
    try:
        config = load_config()
        location = config['location']

        # Use high-resolution model for daily summary
        models_to_try = ['knmi_harmonie_arome_nl', 'dwd_icon_d2']
        model_results = weather_service.fetch_standard_weather_models(
            location['latitude'], location['longitude'], models_to_try
        )

        if model_results:
            # Use the highest resolution model available
            best_model = 'knmi_harmonie_arome_nl' if 'knmi_harmonie_arome_nl' in model_results else 'dwd_icon_d2'
            std = model_results[best_model]
            logger.info(f"Using high-resolution model {best_model} for daily summary")
        else:
            # Fallback to standard method
            std = weather_service.fetch_standard_weather(location['latitude'], location['longitude'], model='knmi_harmonie_arome_nl')

        mar = weather_service.fetch_marine_weather(location['latitude'], location['longitude'])
        if not std or not mar:
            return jsonify({"error": "Failed to fetch weather data"}), 500

        tz_offset_sec = int(std.get('utc_offset_seconds') or 0)
        local_now = datetime.utcnow() + timedelta(seconds=tz_offset_sec)
        local_day = local_now.date()

        hourly = std.get('hourly', {})
        times: List[str] = hourly.get('time') or []
        idx_today: List[int] = []
        for i, t in enumerate(times):
            try:
                dt = dateparser.isoparse(t)
            except Exception:
                continue
            if (dt.date() == local_day):
                idx_today.append(i)

        def pick(arr_key: str, default: float = 0.0) -> List[float]:
            arr = hourly.get(arr_key) or []
            return [float(arr[i]) if i < len(arr) and arr[i] is not None else default for i in idx_today]

        wind_ms = pick('wind_speed_10m', 0.0)
        gust_ms = pick('wind_gusts_10m', 0.0)
        temp_c = pick('temperature_2m', 0.0)

        marine_h = mar.get('hourly', {}).get('wave_height') or []
        marine_t = mar.get('hourly', {}).get('time') or []
        marine_idx = [i for i, t in enumerate(marine_t) if (dateparser.isoparse(t).date() == local_day)]
        waves = [float(marine_h[i]) if i < len(marine_h) and marine_h[i] is not None else 0.0 for i in marine_idx]

        def stats(vals: List[float]) -> Dict[str, float]:
            if not vals:
                return {"min": 0, "max": 0, "avg": 0}
            return {
                "min": round(min(vals), 2),
                "max": round(max(vals), 2),
                "avg": round(sum(vals) / len(vals), 2)
            }

        # Convert to knots for wind
        wind_knots = [v * 1.944 for v in wind_ms]
        gust_knots = [v * 1.944 for v in gust_ms]

        prefs = config.get('wingfoil_preferences', {})
        opt_min = float(prefs.get('optimal_wind_min', 15))
        opt_max = float(prefs.get('optimal_wind_max', 25))
        # Find windows (indices) where wind within optimal range
        windows = []
        start = None
        for i, v in enumerate(wind_knots):
            if opt_min <= v <= opt_max:
                if start is None:
                    start = i
            else:
                if start is not None:
                    windows.append((start, i - 1))
                    start = None
        if start is not None:
            windows.append((start, len(wind_knots) - 1))

        def idx_to_time(i: int) -> str:
            if i < 0 or i >= len(idx_today):
                return ""
            src_i = idx_today[i]
            return times[src_i] if src_i < len(times) else ""

        pretty_windows = [{"from": idx_to_time(a), "to": idx_to_time(b)} for (a, b) in windows]

        # Calculate hourly scores for the day
        hourly_scores = []
        try:
            for i, time_idx in enumerate(idx_today):
                try:
                    time_str = times[time_idx] if time_idx < len(times) else ""
                    if not time_str:
                        continue

                    # Get hourly data
                    wind_speed_ms = wind_ms[i] if i < len(wind_ms) else 0.0
                    wind_speed_knots = wind_knots[i] if i < len(wind_knots) else 0.0
                    wind_dir_val = pick('wind_direction_10m', 180)[i] if i < len(pick('wind_direction_10m', 180)) else 180
                    wind_gust_ms = gust_ms[i] if i < len(gust_ms) else wind_speed_ms
                    temp = temp_c[i] if i < len(temp_c) else 20.0

                    # Marine data
                    marine_index = min(i, len(waves) - 1) if waves else 0
                    wave_height = waves[marine_index] if waves else 0.5
                    wave_period = 5.0  # Default wave period

                    # Create weather conditions for this hour
                    daily_conditions = WeatherConditions(
                        timestamp=time_str,
                        location=location['name'],
                        latitude=location['latitude'],
                        longitude=location['longitude'],
                        wind_speed_ms=wind_speed_ms,
                        wind_speed_knots=wind_speed_knots,
                        wind_direction=int(wind_dir_val),
                        wind_gust_ms=wind_gust_ms,
                        temperature=temp,
                        water_temperature=15.0,  # Default
                        wave_height=wave_height,
                        wave_period=wave_period,
                        wave_direction=180,  # Default
                        pressure=1013.0,  # Default
                        humidity=60,  # Default
                        visibility=10000.0,  # Default
                        uv_index=0.0,  # Default
                        shore_angle_deg=abs(int(wind_dir_val) - location.get('shore_direction', 90)) % 360,
                        chop_index=1.0  # Default
                    )

                    # Calculate comprehensive score
                    wingfoil_conditions_v2 = wingfoil_analyzer.analyze_conditions_v2(
                        daily_conditions,
                        int(location.get('shore_direction', 90)),
                        {}  # No model consensus for daily data
                    )

                    hourly_scores.append({
                        "time": time_str.split('T')[1][:5] if 'T' in time_str else time_str,
                        "timestamp": time_str,
                        "score": wingfoil_conditions_v2['overall_score'],
                        "suitable": wingfoil_conditions_v2['suitable'],
                        "conditions": wingfoil_conditions_v2['conditions'],
                        "wind_knots": round(wind_speed_knots, 1),
                        "wave_height_m": round(wave_height, 1),
                        "temperature_c": round(temp, 1),
                        "pillars": wingfoil_conditions_v2['pillars'],
                        "confidence": wingfoil_conditions_v2['confidence']
                    })

                except Exception as e:
                    logger.warning(f"Error calculating score for daily hour {i}: {e}")
                    continue

        except Exception as e:
            logger.warning(f"Error calculating hourly scores for daily summary: {e}")
            hourly_scores = []

        summary = {
            "day": str(local_day),
            "wind_knots": stats(wind_knots),
            "gust_knots": stats(gust_knots),
            "temperature_c": stats(temp_c),
            "wave_height_m": stats(waves),
            "optimal_windows": pretty_windows,
            "hourly_scores": hourly_scores,
            "scoring_stats": {
                "total_hours": len(hourly_scores),
                "suitable_hours": len([h for h in hourly_scores if h['suitable']]),
                "excellent_hours": len([h for h in hourly_scores if h['score'] >= 85]),
                "good_hours": len([h for h in hourly_scores if 70 <= h['score'] < 85]),
                "marginal_hours": len([h for h in hourly_scores if 60 <= h['score'] < 70]),
                "poor_hours": len([h for h in hourly_scores if h['score'] < 60]),
                "avg_score": round(sum(h['score'] for h in hourly_scores) / len(hourly_scores), 1) if hourly_scores else 0
            } if hourly_scores else None
        }
        return jsonify(summary)
    except Exception as e:
        logger.error(f"Error building daily summary: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/config', methods=['GET', 'POST'])
def handle_config():
    """Get or update configuration"""
    if request.method == 'GET':
        return jsonify(_sanitize_config(load_config()))
    else:
        try:
            if not _require_admin(request):
                return jsonify({"error": "Unauthorized"}), 401
            incoming = request.get_json(force=True, silent=False) or {}
            if not isinstance(incoming, dict):
                return jsonify({"error": "Invalid config payload"}), 400
            config_path = '/app/config/config.json'
            current = load_config()
            # Merge shallowly
            merged = {**current, **incoming}
            with open(config_path, 'w') as f:
                json.dump(merged, f, indent=2)
            init_services()  # reload services with new config
            return jsonify({"message": "Config updated", "config": _sanitize_config(merged)})
        except Exception as e:
            logger.error(f"Error updating config: {e}")
            return jsonify({"error": str(e)}), 500

@app.route('/settings')
def settings_page():
    return render_template('settings.html')

@app.route('/api-help')
def api_help():
    """API documentation page"""
    return render_template('api-help.html')

@app.route('/health')
def health_check():
    """Health check endpoint"""
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "version": "1.0.0"
    })

# Initialize services when module is imported
init_services()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
