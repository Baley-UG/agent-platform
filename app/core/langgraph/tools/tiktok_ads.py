"""TikTok Ads API tools for the marketing agent.

This module provides LangChain tools to interact with the TikTok Ads API,
enabling campaign management, ad group management, analytics retrieval,
and creative intake from Google Drive.
"""

import json
import os
import tempfile
from typing import Optional

import httpx
from langchain_community.tools import DuckDuckGoSearchResults
from langchain_core.tools import tool

from app.core.config import settings
from app.core.logging import logger

# Web search — used for competitor research, TikTok trend discovery
web_search_tool = DuckDuckGoSearchResults(num_results=5, handle_tool_error=True)

TIKTOK_ADS_BASE_URL = "https://business-api.tiktok.com/open_api/v1.3"


def _get_headers() -> dict:
    return {
        "Access-Token": settings.TIKTOK_ACCESS_TOKEN,
        "Content-Type": "application/json",
    }


def _format_response(data: dict) -> str:
    """Format API response as readable JSON string."""
    return json.dumps(data, indent=2, ensure_ascii=False)


@tool
async def tiktok_get_campaigns(status_filter: str = "ALL") -> str:
    """Get all TikTok ad campaigns with their status and budget info.

    Args:
        status_filter: Filter by status - ALL, ENABLE (active), DISABLE (paused), DELETE

    Returns:
        JSON string with campaign list (id, name, status, budget, objective, create_time)
    """
    try:
        params = {
            "advertiser_id": settings.TIKTOK_ADVERTISER_ID,
            "page_size": 20,
        }
        if status_filter != "ALL":
            params["primary_status"] = status_filter

        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                f"{TIKTOK_ADS_BASE_URL}/campaign/get/",
                headers=_get_headers(),
                params=params,
            )
            response.raise_for_status()
            data = response.json()

        if data.get("code") != 0:
            logger.error("tiktok_get_campaigns_api_error", code=data.get("code"), message=data.get("message"))
            return f"TikTok API error: {data.get('message', 'Unknown error')}"

        campaigns = data.get("data", {}).get("list", [])
        if not campaigns:
            return "No campaigns found."

        result = [
            {
                "id": c.get("campaign_id"),
                "name": c.get("campaign_name"),
                "status": c.get("primary_status"),
                "objective": c.get("objective_type"),
                "budget": c.get("budget"),
                "budget_mode": c.get("budget_mode"),
                "create_time": c.get("create_time"),
            }
            for c in campaigns
        ]
        logger.info("tiktok_get_campaigns_success", count=len(result))
        return _format_response({"campaigns": result, "total": len(result)})
    except httpx.HTTPError as e:
        logger.exception("tiktok_get_campaigns_http_error", error=str(e))
        return f"HTTP error fetching campaigns: {str(e)}"
    except Exception as e:
        logger.exception("tiktok_get_campaigns_error", error=str(e))
        return f"Error fetching campaigns: {str(e)}"


@tool
async def tiktok_create_campaign(
    name: str,
    objective: str,
    budget: float,
    budget_mode: str = "BUDGET_MODE_DAY",
) -> str:
    """Create a new TikTok ad campaign.

    Args:
        name: Campaign name (must be unique)
        objective: Campaign objective - REACH, TRAFFIC, VIDEO_VIEWS, CONVERSIONS, APP_PROMOTION, LEAD_GENERATION
        budget: Budget amount in account currency
        budget_mode: BUDGET_MODE_DAY (daily budget) or BUDGET_MODE_TOTAL (lifetime budget)

    Returns:
        JSON string with created campaign_id and status
    """
    try:
        payload = {
            "advertiser_id": settings.TIKTOK_ADVERTISER_ID,
            "campaign_name": name,
            "objective_type": objective,
            "budget_mode": budget_mode,
            "budget": budget,
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                f"{TIKTOK_ADS_BASE_URL}/campaign/create/",
                headers=_get_headers(),
                json=payload,
            )
            response.raise_for_status()
            data = response.json()

        if data.get("code") != 0:
            logger.error("tiktok_create_campaign_api_error", code=data.get("code"), message=data.get("message"))
            return f"TikTok API error: {data.get('message', 'Unknown error')}"

        campaign_id = data.get("data", {}).get("campaign_id")
        logger.info("tiktok_create_campaign_success", campaign_id=campaign_id, name=name)
        return _format_response(
            {
                "success": True,
                "campaign_id": campaign_id,
                "name": name,
                "objective": objective,
                "budget": budget,
                "budget_mode": budget_mode,
            }
        )
    except httpx.HTTPError as e:
        logger.exception("tiktok_create_campaign_http_error", error=str(e))
        return f"HTTP error creating campaign: {str(e)}"
    except Exception as e:
        logger.exception("tiktok_create_campaign_error", error=str(e))
        return f"Error creating campaign: {str(e)}"


@tool
async def tiktok_update_campaign_status(campaign_id: str, status: str) -> str:
    """Pause, activate or delete a TikTok ad campaign.

    Args:
        campaign_id: The campaign ID to update
        status: New status - ENABLE (activate), DISABLE (pause), DELETE

    Returns:
        JSON string confirming the status change
    """
    valid_statuses = {"ENABLE", "DISABLE", "DELETE"}
    if status not in valid_statuses:
        return f"Invalid status '{status}'. Must be one of: {', '.join(valid_statuses)}"

    try:
        payload = {
            "advertiser_id": settings.TIKTOK_ADVERTISER_ID,
            "campaign_ids": [campaign_id],
            "opt_status": status,
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                f"{TIKTOK_ADS_BASE_URL}/campaign/status/update/",
                headers=_get_headers(),
                json=payload,
            )
            response.raise_for_status()
            data = response.json()

        if data.get("code") != 0:
            logger.error(
                "tiktok_update_campaign_status_api_error", code=data.get("code"), message=data.get("message")
            )
            return f"TikTok API error: {data.get('message', 'Unknown error')}"

        logger.info("tiktok_update_campaign_status_success", campaign_id=campaign_id, status=status)
        return _format_response({"success": True, "campaign_id": campaign_id, "new_status": status})
    except httpx.HTTPError as e:
        logger.exception("tiktok_update_campaign_status_http_error", error=str(e))
        return f"HTTP error updating campaign status: {str(e)}"
    except Exception as e:
        logger.exception("tiktok_update_campaign_status_error", error=str(e))
        return f"Error updating campaign status: {str(e)}"


@tool
async def tiktok_update_campaign_budget(campaign_id: str, budget: float, budget_mode: str = "BUDGET_MODE_DAY") -> str:
    """Update the budget of a TikTok ad campaign.

    Args:
        campaign_id: The campaign ID to update
        budget: New budget amount in account currency
        budget_mode: BUDGET_MODE_DAY (daily) or BUDGET_MODE_TOTAL (lifetime)

    Returns:
        JSON string confirming the budget change
    """
    try:
        payload = {
            "advertiser_id": settings.TIKTOK_ADVERTISER_ID,
            "campaign_id": campaign_id,
            "budget": budget,
            "budget_mode": budget_mode,
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                f"{TIKTOK_ADS_BASE_URL}/campaign/update/",
                headers=_get_headers(),
                json=payload,
            )
            response.raise_for_status()
            data = response.json()

        if data.get("code") != 0:
            logger.error("tiktok_update_campaign_budget_api_error", code=data.get("code"), message=data.get("message"))
            return f"TikTok API error: {data.get('message', 'Unknown error')}"

        logger.info("tiktok_update_campaign_budget_success", campaign_id=campaign_id, budget=budget)
        return _format_response(
            {"success": True, "campaign_id": campaign_id, "new_budget": budget, "budget_mode": budget_mode}
        )
    except httpx.HTTPError as e:
        logger.exception("tiktok_update_campaign_budget_http_error", error=str(e))
        return f"HTTP error updating campaign budget: {str(e)}"
    except Exception as e:
        logger.exception("tiktok_update_campaign_budget_error", error=str(e))
        return f"Error updating campaign budget: {str(e)}"


@tool
async def tiktok_get_analytics(
    campaign_id: str,
    start_date: str,
    end_date: str,
    metrics: Optional[str] = None,
) -> str:
    """Get performance analytics for a TikTok ad campaign.

    Args:
        campaign_id: The campaign ID to get analytics for
        start_date: Start date in YYYY-MM-DD format
        end_date: End date in YYYY-MM-DD format
        metrics: Comma-separated metrics to retrieve. Defaults to spend, impressions, clicks, ctr, cpc, cpm, conversions, roas

    Returns:
        JSON string with daily performance metrics
    """
    default_metrics = [
        "spend",
        "impressions",
        "clicks",
        "ctr",
        "cpc",
        "cpm",
        "conversion",
        "cost_per_conversion",
        "real_time_conversion",
    ]
    requested_metrics = metrics.split(",") if metrics else default_metrics

    try:
        payload = {
            "advertiser_id": settings.TIKTOK_ADVERTISER_ID,
            "report_type": "BASIC",
            "dimensions": ["campaign_id", "stat_time_day"],
            "metrics": requested_metrics,
            "data_level": "AUCTION_CAMPAIGN",
            "filters": [{"field_name": "campaign_id", "filter_type": "IN", "filter_value": f'["{campaign_id}"]'}],
            "start_date": start_date,
            "end_date": end_date,
            "page_size": 30,
        }

        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                f"{TIKTOK_ADS_BASE_URL}/report/integrated/get/",
                headers=_get_headers(),
                json=payload,
            )
            response.raise_for_status()
            data = response.json()

        if data.get("code") != 0:
            logger.error("tiktok_get_analytics_api_error", code=data.get("code"), message=data.get("message"))
            return f"TikTok API error: {data.get('message', 'Unknown error')}"

        rows = data.get("data", {}).get("list", [])
        logger.info("tiktok_get_analytics_success", campaign_id=campaign_id, rows=len(rows))
        return _format_response(
            {
                "campaign_id": campaign_id,
                "period": {"start": start_date, "end": end_date},
                "data": rows,
            }
        )
    except httpx.HTTPError as e:
        logger.exception("tiktok_get_analytics_http_error", error=str(e))
        return f"HTTP error fetching analytics: {str(e)}"
    except Exception as e:
        logger.exception("tiktok_get_analytics_error", error=str(e))
        return f"Error fetching analytics: {str(e)}"


@tool
async def tiktok_list_ad_groups(campaign_id: str) -> str:
    """List all ad groups within a TikTok campaign.

    Args:
        campaign_id: The campaign ID to list ad groups for

    Returns:
        JSON string with ad group list (id, name, status, budget, targeting summary)
    """
    try:
        params = {
            "advertiser_id": settings.TIKTOK_ADVERTISER_ID,
            "campaign_ids": f'["{campaign_id}"]',
            "page_size": 20,
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                f"{TIKTOK_ADS_BASE_URL}/adgroup/get/",
                headers=_get_headers(),
                params=params,
            )
            response.raise_for_status()
            data = response.json()

        if data.get("code") != 0:
            logger.error("tiktok_list_ad_groups_api_error", code=data.get("code"), message=data.get("message"))
            return f"TikTok API error: {data.get('message', 'Unknown error')}"

        ad_groups = data.get("data", {}).get("list", [])
        if not ad_groups:
            return f"No ad groups found for campaign {campaign_id}."

        result = [
            {
                "id": ag.get("adgroup_id"),
                "name": ag.get("adgroup_name"),
                "status": ag.get("primary_status"),
                "budget": ag.get("budget"),
                "bid_type": ag.get("bid_type"),
                "bid": ag.get("bid"),
                "location": ag.get("location_ids"),
                "age": ag.get("age_groups"),
                "gender": ag.get("gender"),
            }
            for ag in ad_groups
        ]
        logger.info("tiktok_list_ad_groups_success", campaign_id=campaign_id, count=len(result))
        return _format_response({"campaign_id": campaign_id, "ad_groups": result, "total": len(result)})
    except httpx.HTTPError as e:
        logger.exception("tiktok_list_ad_groups_http_error", error=str(e))
        return f"HTTP error listing ad groups: {str(e)}"
    except Exception as e:
        logger.exception("tiktok_list_ad_groups_error", error=str(e))
        return f"Error listing ad groups: {str(e)}"


@tool
async def tiktok_create_ad_group(
    campaign_id: str,
    name: str,
    budget: float,
    budget_mode: str,
    bid: float,
    placement: str = "PLACEMENT_TIKTOK",
    age_groups: Optional[str] = None,
    gender: str = "GENDER_UNLIMITED",
    location_ids: Optional[str] = None,
) -> str:
    """Create a new ad group inside a TikTok campaign.

    Args:
        campaign_id: Parent campaign ID
        name: Ad group name
        budget: Daily or total budget
        budget_mode: BUDGET_MODE_DAY or BUDGET_MODE_TOTAL
        bid: Bid amount per optimization event
        placement: Placement type - PLACEMENT_TIKTOK (default), PLACEMENT_PANGLE, PLACEMENT_GLOBAL_APP_BUNDLE
        age_groups: Comma-separated age groups - AGE_13_17, AGE_18_24, AGE_25_34, AGE_35_44, AGE_45_54, AGE_55_100
        gender: GENDER_UNLIMITED, GENDER_MALE, GENDER_FEMALE
        location_ids: Comma-separated TikTok location IDs (e.g. "6252001" for USA)

    Returns:
        JSON string with created ad group ID
    """
    try:
        payload: dict = {
            "advertiser_id": settings.TIKTOK_ADVERTISER_ID,
            "campaign_id": campaign_id,
            "adgroup_name": name,
            "budget_mode": budget_mode,
            "budget": budget,
            "bid": bid,
            "placements": [placement],
            "gender": gender,
            "bid_type": "BID_TYPE_NO_BID",
            "optimization_goal": "CLICK",
        }

        if age_groups:
            payload["age_groups"] = [a.strip() for a in age_groups.split(",")]

        if location_ids:
            payload["location_ids"] = [loc.strip() for loc in location_ids.split(",")]

        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                f"{TIKTOK_ADS_BASE_URL}/adgroup/create/",
                headers=_get_headers(),
                json=payload,
            )
            response.raise_for_status()
            data = response.json()

        if data.get("code") != 0:
            logger.error("tiktok_create_ad_group_api_error", code=data.get("code"), message=data.get("message"))
            return f"TikTok API error: {data.get('message', 'Unknown error')}"

        adgroup_id = data.get("data", {}).get("adgroup_id")
        logger.info("tiktok_create_ad_group_success", adgroup_id=adgroup_id, campaign_id=campaign_id)
        return _format_response(
            {
                "success": True,
                "adgroup_id": adgroup_id,
                "name": name,
                "campaign_id": campaign_id,
                "budget": budget,
                "bid": bid,
            }
        )
    except httpx.HTTPError as e:
        logger.exception("tiktok_create_ad_group_http_error", error=str(e))
        return f"HTTP error creating ad group: {str(e)}"
    except Exception as e:
        logger.exception("tiktok_create_ad_group_error", error=str(e))
        return f"Error creating ad group: {str(e)}"


@tool
async def tiktok_list_ads(adgroup_id: str) -> str:
    """List all ads within a TikTok ad group.

    Args:
        adgroup_id: The ad group ID to list ads for

    Returns:
        JSON string with ad list (id, name, status, video_id, call_to_action, landing_page_url)
    """
    try:
        params = {
            "advertiser_id": settings.TIKTOK_ADVERTISER_ID,
            "adgroup_ids": f'["{adgroup_id}"]',
            "page_size": 20,
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                f"{TIKTOK_ADS_BASE_URL}/ad/get/",
                headers=_get_headers(),
                params=params,
            )
            response.raise_for_status()
            data = response.json()

        if data.get("code") != 0:
            logger.error("tiktok_list_ads_api_error", code=data.get("code"), message=data.get("message"))
            return f"TikTok API error: {data.get('message', 'Unknown error')}"

        ads = data.get("data", {}).get("list", [])
        if not ads:
            return f"No ads found for ad group {adgroup_id}."

        result = [
            {
                "id": ad.get("ad_id"),
                "name": ad.get("ad_name"),
                "status": ad.get("primary_status"),
                "video_id": ad.get("video_id"),
                "call_to_action": ad.get("call_to_action"),
                "landing_page_url": ad.get("landing_page_url"),
                "create_time": ad.get("create_time"),
            }
            for ad in ads
        ]
        logger.info("tiktok_list_ads_success", adgroup_id=adgroup_id, count=len(result))
        return _format_response({"adgroup_id": adgroup_id, "ads": result, "total": len(result)})
    except httpx.HTTPError as e:
        logger.exception("tiktok_list_ads_http_error", error=str(e))
        return f"HTTP error listing ads: {str(e)}"
    except Exception as e:
        logger.exception("tiktok_list_ads_error", error=str(e))
        return f"Error listing ads: {str(e)}"


@tool
async def tiktok_update_ad_status(ad_id: str, adgroup_id: str, status: str) -> str:
    """Pause, activate or delete a specific TikTok ad creative.

    Args:
        ad_id: The ad ID to update
        adgroup_id: The ad group this ad belongs to
        status: New status - ENABLE (activate), DISABLE (pause), DELETE

    Returns:
        JSON string confirming the status change
    """
    valid_statuses = {"ENABLE", "DISABLE", "DELETE"}
    if status not in valid_statuses:
        return f"Invalid status '{status}'. Must be one of: {', '.join(valid_statuses)}"

    try:
        payload = {
            "advertiser_id": settings.TIKTOK_ADVERTISER_ID,
            "adgroup_id": adgroup_id,
            "ad_ids": [ad_id],
            "opt_status": status,
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                f"{TIKTOK_ADS_BASE_URL}/ad/status/update/",
                headers=_get_headers(),
                json=payload,
            )
            response.raise_for_status()
            data = response.json()

        if data.get("code") != 0:
            logger.error("tiktok_update_ad_status_api_error", code=data.get("code"), message=data.get("message"))
            return f"TikTok API error: {data.get('message', 'Unknown error')}"

        logger.info("tiktok_update_ad_status_success", ad_id=ad_id, status=status)
        return _format_response({"success": True, "ad_id": ad_id, "new_status": status})
    except httpx.HTTPError as e:
        logger.exception("tiktok_update_ad_status_http_error", error=str(e))
        return f"HTTP error updating ad status: {str(e)}"
    except Exception as e:
        logger.exception("tiktok_update_ad_status_error", error=str(e))
        return f"Error updating ad status: {str(e)}"


@tool
async def tiktok_get_account_info() -> str:
    """Get advertiser account info including balance, currency, timezone, and account status.

    Returns:
        JSON string with account details (name, balance, currency, timezone, status)
    """
    try:
        params = {"advertiser_ids": f'["{settings.TIKTOK_ADVERTISER_ID}"]'}
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                f"{TIKTOK_ADS_BASE_URL}/advertiser/info/",
                headers=_get_headers(),
                params=params,
            )
            response.raise_for_status()
            data = response.json()

        if data.get("code") != 0:
            logger.error("tiktok_get_account_info_api_error", code=data.get("code"), message=data.get("message"))
            return f"TikTok API error: {data.get('message', 'Unknown error')}"

        advertisers = data.get("data", {}).get("list", [])
        if not advertisers:
            return "No advertiser info found."

        adv = advertisers[0]
        result = {
            "name": adv.get("name"),
            "advertiser_id": adv.get("advertiser_id"),
            "balance": adv.get("balance"),
            "currency": adv.get("currency"),
            "timezone": adv.get("timezone"),
            "status": adv.get("status"),
            "industry": adv.get("industry"),
            "country": adv.get("country"),
        }
        logger.info("tiktok_get_account_info_success", advertiser_id=settings.TIKTOK_ADVERTISER_ID)
        return _format_response(result)
    except httpx.HTTPError as e:
        logger.exception("tiktok_get_account_info_http_error", error=str(e))
        return f"HTTP error fetching account info: {str(e)}"
    except Exception as e:
        logger.exception("tiktok_get_account_info_error", error=str(e))
        return f"Error fetching account info: {str(e)}"


@tool
async def tiktok_get_audience_insights(
    campaign_id: str,
    start_date: str,
    end_date: str,
) -> str:
    """Get demographic and device breakdown for a campaign (age, gender, device, placement).

    Args:
        campaign_id: The campaign ID to analyze
        start_date: Start date in YYYY-MM-DD format
        end_date: End date in YYYY-MM-DD format

    Returns:
        JSON string with performance broken down by age, gender, device type
    """
    breakdown_dimensions = [
        ("age", ["age", "stat_time_day"]),
        ("gender", ["gender", "stat_time_day"]),
        ("device", ["device_type", "stat_time_day"]),
    ]
    results = {}
    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            for label, dimensions in breakdown_dimensions:
                payload = {
                    "advertiser_id": settings.TIKTOK_ADVERTISER_ID,
                    "report_type": "AUDIENCE",
                    "dimensions": dimensions,
                    "metrics": ["spend", "impressions", "clicks", "ctr", "cpc"],
                    "data_level": "AUCTION_CAMPAIGN",
                    "filters": [
                        {"field_name": "campaign_id", "filter_type": "IN", "filter_value": f'["{campaign_id}"]'}
                    ],
                    "start_date": start_date,
                    "end_date": end_date,
                    "page_size": 20,
                }
                response = await client.post(
                    f"{TIKTOK_ADS_BASE_URL}/report/integrated/get/",
                    headers=_get_headers(),
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()
                if data.get("code") == 0:
                    results[label] = data.get("data", {}).get("list", [])
                else:
                    results[label] = f"Error: {data.get('message')}"

        logger.info("tiktok_get_audience_insights_success", campaign_id=campaign_id)
        return _format_response(
            {"campaign_id": campaign_id, "period": {"start": start_date, "end": end_date}, "breakdown": results}
        )
    except httpx.HTTPError as e:
        logger.exception("tiktok_get_audience_insights_http_error", error=str(e))
        return f"HTTP error fetching audience insights: {str(e)}"
    except Exception as e:
        logger.exception("tiktok_get_audience_insights_error", error=str(e))
        return f"Error fetching audience insights: {str(e)}"


@tool
async def tiktok_bulk_update_campaign_status(campaign_ids: str, status: str) -> str:
    """Pause or activate multiple TikTok campaigns at once.

    Args:
        campaign_ids: Comma-separated list of campaign IDs
        status: New status - ENABLE (activate) or DISABLE (pause)

    Returns:
        JSON string with per-campaign result
    """
    valid_statuses = {"ENABLE", "DISABLE"}
    if status not in valid_statuses:
        return f"Invalid status '{status}'. Must be ENABLE or DISABLE."

    ids = [cid.strip() for cid in campaign_ids.split(",") if cid.strip()]
    if not ids:
        return "No campaign IDs provided."

    try:
        payload = {
            "advertiser_id": settings.TIKTOK_ADVERTISER_ID,
            "campaign_ids": ids,
            "opt_status": status,
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                f"{TIKTOK_ADS_BASE_URL}/campaign/status/update/",
                headers=_get_headers(),
                json=payload,
            )
            response.raise_for_status()
            data = response.json()

        if data.get("code") != 0:
            logger.error(
                "tiktok_bulk_update_campaign_status_api_error",
                code=data.get("code"),
                message=data.get("message"),
            )
            return f"TikTok API error: {data.get('message', 'Unknown error')}"

        logger.info("tiktok_bulk_update_campaign_status_success", count=len(ids), status=status)
        return _format_response({"success": True, "updated_campaigns": ids, "new_status": status})
    except httpx.HTTPError as e:
        logger.exception("tiktok_bulk_update_campaign_status_http_error", error=str(e))
        return f"HTTP error bulk updating campaigns: {str(e)}"
    except Exception as e:
        logger.exception("tiktok_bulk_update_campaign_status_error", error=str(e))
        return f"Error bulk updating campaigns: {str(e)}"


@tool
def analyze_campaign_performance(analytics_json: str) -> str:
    """Analyze raw campaign analytics data and return a structured health assessment.

    This is a local computation tool — no API call needed.
    Pass the JSON string returned by tiktok_get_analytics directly.

    Args:
        analytics_json: JSON string from tiktok_get_analytics tool

    Returns:
        JSON string with health score (0-100), issues found, and specific recommendations
    """
    import json as _json

    try:
        data = _json.loads(analytics_json)
    except _json.JSONDecodeError:
        return "Invalid JSON input. Pass the raw output from tiktok_get_analytics."

    rows = data.get("data", [])
    if not rows:
        return _format_response({"health_score": 0, "issues": ["No data available for the given period."], "recommendations": []})

    # Aggregate totals across days
    totals: dict = {
        "spend": 0.0, "impressions": 0, "clicks": 0,
        "conversion": 0, "ctr_sum": 0.0, "cpc_sum": 0.0,
        "cpm_sum": 0.0, "days": 0,
    }
    for row in rows:
        m = row.get("metrics", row)  # handle flat or nested format
        totals["spend"] += float(m.get("spend", 0) or 0)
        totals["impressions"] += int(m.get("impressions", 0) or 0)
        totals["clicks"] += int(m.get("clicks", 0) or 0)
        totals["conversion"] += int(m.get("conversion", 0) or 0)
        totals["ctr_sum"] += float(m.get("ctr", 0) or 0)
        totals["cpc_sum"] += float(m.get("cpc", 0) or 0)
        totals["cpm_sum"] += float(m.get("cpm", 0) or 0)
        totals["days"] += 1

    days = max(totals["days"], 1)
    avg_ctr = totals["ctr_sum"] / days
    avg_cpc = totals["cpc_sum"] / days
    avg_cpm = totals["cpm_sum"] / days
    cvr = (totals["conversion"] / totals["clicks"] * 100) if totals["clicks"] > 0 else 0

    issues = []
    recommendations = []
    score = 100

    # CTR check (TikTok benchmark ~1.0-2.0%)
    if avg_ctr < 0.5:
        issues.append(f"Very low CTR: {avg_ctr:.2f}% (benchmark: 1-2%)")
        recommendations.append("Refresh ad creatives — hook within first 3 seconds is critical on TikTok.")
        recommendations.append("Test a UGC-style video instead of polished production.")
        score -= 25
    elif avg_ctr < 1.0:
        issues.append(f"Below-average CTR: {avg_ctr:.2f}%")
        recommendations.append("A/B test the opening scene and call-to-action text.")
        score -= 10

    # CPM check (high CPM = audience too narrow or competitive period)
    if avg_cpm > 15:
        issues.append(f"High CPM: ${avg_cpm:.2f} (expected <$10)")
        recommendations.append("Broaden audience targeting — remove overly specific interest layers.")
        score -= 10

    # CVR check
    if totals["clicks"] > 100 and cvr < 1.0:
        issues.append(f"Low conversion rate: {cvr:.2f}%")
        recommendations.append("Review landing page speed and mobile UX — TikTok traffic is 95% mobile.")
        recommendations.append("Add a stronger CTA on the landing page above the fold.")
        score -= 15

    # Spend check (no spend = delivery issue)
    if totals["spend"] == 0:
        issues.append("Zero spend — campaign may not be delivering.")
        recommendations.append("Check campaign status, budget, and bid settings.")
        score -= 30

    # Creative fatigue check (>7 days of data, CTR declining)
    if days >= 7:
        first_half = rows[: days // 2]
        second_half = rows[days // 2:]
        first_ctr = sum(float((r.get("metrics", r)).get("ctr", 0) or 0) for r in first_half) / len(first_half)
        second_ctr = sum(float((r.get("metrics", r)).get("ctr", 0) or 0) for r in second_half) / len(second_half)
        if second_ctr < first_ctr * 0.7:
            issues.append(f"Creative fatigue detected: CTR dropped {((first_ctr - second_ctr) / first_ctr * 100):.0f}% in second half")
            recommendations.append("Introduce new creative variants — rotate at least 3-5 ad versions.")
            score -= 15

    health_label = "Excellent" if score >= 85 else "Good" if score >= 70 else "Needs Attention" if score >= 50 else "Critical"

    return _format_response({
        "health_score": max(score, 0),
        "health_label": health_label,
        "summary": {
            "total_spend": round(totals["spend"], 2),
            "total_impressions": totals["impressions"],
            "total_clicks": totals["clicks"],
            "total_conversions": totals["conversion"],
            "avg_ctr_pct": round(avg_ctr, 3),
            "avg_cpc_usd": round(avg_cpc, 2),
            "avg_cpm_usd": round(avg_cpm, 2),
            "conversion_rate_pct": round(cvr, 2),
            "days_analyzed": days,
        },
        "issues": issues if issues else ["No major issues detected."],
        "recommendations": recommendations if recommendations else ["Campaign is performing well. Consider scaling budget by 20-30%."],
    })


@tool
def generate_campaign_report(
    campaign_name: str,
    analytics_json: str,
    audience_json: Optional[str] = None,
) -> str:
    """Generate a formatted Markdown performance report for a TikTok campaign.

    Combine with tiktok_get_analytics and optionally tiktok_get_audience_insights.
    This is a local computation tool — no API call needed.

    Args:
        campaign_name: Human-readable name for the report header
        analytics_json: JSON string from tiktok_get_analytics
        audience_json: Optional JSON string from tiktok_get_audience_insights

    Returns:
        A formatted Markdown report suitable for sharing in Slack or saving as a file
    """
    import json as _json
    from datetime import datetime as _dt

    try:
        analytics = _json.loads(analytics_json)
    except _json.JSONDecodeError:
        return "Invalid analytics JSON."

    rows = analytics.get("data", [])
    period = analytics.get("period", {})

    totals: dict = {"spend": 0.0, "impressions": 0, "clicks": 0, "conversion": 0}
    for row in rows:
        m = row.get("metrics", row)
        totals["spend"] += float(m.get("spend", 0) or 0)
        totals["impressions"] += int(m.get("impressions", 0) or 0)
        totals["clicks"] += int(m.get("clicks", 0) or 0)
        totals["conversion"] += int(m.get("conversion", 0) or 0)

    ctr = (totals["clicks"] / totals["impressions"] * 100) if totals["impressions"] > 0 else 0
    cpc = (totals["spend"] / totals["clicks"]) if totals["clicks"] > 0 else 0
    cpm = (totals["spend"] / totals["impressions"] * 1000) if totals["impressions"] > 0 else 0
    cvr = (totals["conversion"] / totals["clicks"] * 100) if totals["clicks"] > 0 else 0
    cpp = (totals["spend"] / totals["conversion"]) if totals["conversion"] > 0 else 0

    report_lines = [
        f"# 📊 TikTok Campaign Report: {campaign_name}",
        f"**Period:** {period.get('start', 'N/A')} → {period.get('end', 'N/A')}",
        f"**Generated:** {_dt.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "## 💰 Spend & Reach",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Total Spend | ${totals['spend']:,.2f} |",
        f"| Impressions | {totals['impressions']:,} |",
        f"| CPM | ${cpm:.2f} |",
        "",
        "## 🖱️ Engagement",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Clicks | {totals['clicks']:,} |",
        f"| CTR | {ctr:.2f}% |",
        f"| CPC | ${cpc:.2f} |",
        "",
        "## 🎯 Conversions",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Conversions | {totals['conversion']:,} |",
        f"| CVR | {cvr:.2f}% |",
        f"| Cost per Conversion | ${cpp:.2f} |",
    ]

    if audience_json:
        try:
            audience = _json.loads(audience_json)
            breakdown = audience.get("breakdown", {})
            if breakdown.get("age"):
                report_lines += ["", "## 👥 Top Age Groups (by spend)"]
                sorted_ages = sorted(
                    breakdown["age"],
                    key=lambda x: float((x.get("metrics", x)).get("spend", 0) or 0),
                    reverse=True,
                )[:3]
                for item in sorted_ages:
                    m = item.get("metrics", item)
                    dims = item.get("dimensions", {})
                    report_lines.append(f"- **{dims.get('age', 'N/A')}**: ${float(m.get('spend', 0) or 0):.2f} spend, {float(m.get('ctr', 0) or 0):.2f}% CTR")
        except Exception:
            pass

    report_lines += ["", "---", "_Report generated by Baley Marketing Agent_"]
    return "\n".join(report_lines)


@tool
async def google_drive_list_files(
    folder_id: Optional[str] = None,
    mime_prefix: str = "image/",
    page_size: int = 10,
) -> str:
    """List files in a Google Drive folder (requires GOOGLE_DRIVE_ACCESS_TOKEN env).

    Args:
        folder_id: Google Drive folder ID. If omitted, uses GOOGLE_DRIVE_FOLDER_ID.
        mime_prefix: Filter by MIME type prefix (e.g., image/, video/).
        page_size: Max files to return.

    Returns:
        JSON string with file id, name, mimeType, size, modifiedTime.
    """
    token = settings.GOOGLE_DRIVE_ACCESS_TOKEN
    folder = folder_id or settings.GOOGLE_DRIVE_FOLDER_ID
    if not token:
        return "GOOGLE_DRIVE_ACCESS_TOKEN is not set."
    if not folder:
        return "Google Drive folder_id missing. Pass folder_id or set GOOGLE_DRIVE_FOLDER_ID."

    query = f"'{folder}' in parents and mimeType contains '{mime_prefix}' and trashed = false"
    params = {
        "q": query,
        "pageSize": page_size,
        "fields": "files(id,name,mimeType,size,modifiedTime)",
        "orderBy": "modifiedTime desc",
    }
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get("https://www.googleapis.com/drive/v3/files", params=params, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        return _format_response({"files": data.get("files", []), "total": len(data.get("files", []))})
    except httpx.HTTPError as e:
        logger.exception("gdrive_list_files_http_error", error=str(e))
        return f"HTTP error listing Drive files: {str(e)}"
    except Exception as e:
        logger.exception("gdrive_list_files_error", error=str(e))
        return f"Error listing Drive files: {str(e)}"


@tool
async def tiktok_upload_image_from_drive(
    file_id: str,
    file_name: Optional[str] = None,
) -> str:
    """Download an image from Google Drive and upload it to TikTok Ads as a creative.

    Requires GOOGLE_DRIVE_ACCESS_TOKEN. Returns TikTok image_id on success.
    """
    drive_token = settings.GOOGLE_DRIVE_ACCESS_TOKEN
    if not drive_token:
        return "GOOGLE_DRIVE_ACCESS_TOKEN is not set."

    headers = {"Authorization": f"Bearer {drive_token}"}
    download_url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            file_resp = await client.get(download_url, headers=headers)
            file_resp.raise_for_status()
            content = file_resp.content
            fname = file_name or f"drive_{file_id}.bin"

        files = {
            "image_file": (fname, content, "application/octet-stream"),
        }
        data = {"advertiser_id": settings.TIKTOK_ADVERTISER_ID}

        async with httpx.AsyncClient(timeout=30.0) as client:
            upload_resp = await client.post(
                f"{TIKTOK_ADS_BASE_URL}/file/image/ad/upload/",
                headers=_get_headers(),
                data=data,
                files=files,
            )
            upload_resp.raise_for_status()
            up_json = upload_resp.json()

        if up_json.get("code") != 0:
            logger.error("tiktok_image_upload_api_error", code=up_json.get("code"), message=up_json.get("message"))
            return f"TikTok API error: {up_json.get('message', 'Unknown error')}"

        image_id = up_json.get("data", {}).get("image_id")
        logger.info("tiktok_image_upload_success", image_id=image_id)
        return _format_response({"success": True, "image_id": image_id, "file_id": file_id, "file_name": fname})
    except httpx.HTTPError as e:
        logger.exception("tiktok_image_upload_http_error", error=str(e))
        return f"HTTP error uploading image: {str(e)}"
    except Exception as e:
        logger.exception("tiktok_image_upload_error", error=str(e))
        return f"Error uploading image: {str(e)}"


@tool
async def tiktok_create_image_ad(
    adgroup_id: str,
    ad_name: str,
    image_id: str,
    landing_page_url: str,
    call_to_action: str = "SHOP_NOW",
    ad_text: str = "",
) -> str:
    """Create a SINGLE_IMAGE ad using a previously uploaded image_id.

    Args:
        adgroup_id: Target ad group ID
        ad_name: Name of the ad
        image_id: TikTok image_id from upload
        landing_page_url: Destination URL
        call_to_action: CTA enum, e.g., SHOP_NOW, LEARN_MORE, SIGN_UP
        ad_text: Optional primary text
    """
    payload = {
        "advertiser_id": settings.TIKTOK_ADVERTISER_ID,
        "adgroup_id": adgroup_id,
        "ad_name": ad_name,
        "creative_material_mode": "SINGLE_IMAGE",
        "image_ids": [image_id],
        "landing_page_url": landing_page_url,
        "call_to_action": call_to_action,
    }
    if ad_text:
        payload["ad_text"] = ad_text

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                f"{TIKTOK_ADS_BASE_URL}/ad/create/",
                headers=_get_headers(),
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()

        if data.get("code") != 0:
            logger.error("tiktok_create_image_ad_api_error", code=data.get("code"), message=data.get("message"))
            return f"TikTok API error: {data.get('message', 'Unknown error')}"

        ad_id = data.get("data", {}).get("ad_id")
        logger.info("tiktok_create_image_ad_success", ad_id=ad_id, adgroup_id=adgroup_id)
        return _format_response({"success": True, "ad_id": ad_id, "adgroup_id": adgroup_id, "image_id": image_id})
    except httpx.HTTPError as e:
        logger.exception("tiktok_create_image_ad_http_error", error=str(e))
        return f"HTTP error creating ad: {str(e)}"
    except Exception as e:
        logger.exception("tiktok_create_image_ad_error", error=str(e))
        return f"Error creating ad: {str(e)}"


tiktok_ads_tools = [
    # Account
    tiktok_get_account_info,
    # Google Drive intake
    google_drive_list_files,
    tiktok_upload_image_from_drive,
    # Ad creation (image)
    tiktok_create_image_ad,
    # Campaign management
    tiktok_get_campaigns,
    tiktok_create_campaign,
    tiktok_update_campaign_status,
    tiktok_bulk_update_campaign_status,
    tiktok_update_campaign_budget,
    # Ad group management
    tiktok_list_ad_groups,
    tiktok_create_ad_group,
    # Ad creative management
    tiktok_list_ads,
    tiktok_update_ad_status,
    # Analytics & insights
    tiktok_get_analytics,
    tiktok_get_audience_insights,
    # Local analysis tools (no API call)
    analyze_campaign_performance,
    generate_campaign_report,
    # Web / competitor research
    web_search_tool,
]
