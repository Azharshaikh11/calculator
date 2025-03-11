from datetime import datetime, timedelta, timezone
import math
from typing import List, Dict, Any, Optional, Set
import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
import asyncio
from fastapi.middleware.cors import CORSMiddleware
import pymongo
from pymongo import MongoClient

app = FastAPI()

# CORS configuration
origins = [
    "http://localhost:3000",  # Adjust this to your frontend URL
    "http://127.0.0.1:5500",
    "http://localhost:5500",
    "https://magnificent-bunny-3a8bb2.netlify.app",
    "https://majestic-naiad-9d6185.netlify.app"
    # Add other origins as needed
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,  # Allows specified origins
    allow_credentials=True,
    allow_methods=["*"],  # Allows all methods (GET, POST, etc.)
    allow_headers=["*"],  # Allows all headers
)

# Global variables
max_capacity: List = []
cap_wise_rates: Dict = {}
weekend_locations: List = []
weekday_locations: List = []
charges: Dict = {}

# Define IST timezone (UTC+5:30)
IST = timezone(timedelta(hours=5, minutes=30))

# MongoDB setup
client = MongoClient("mongodb+srv://data_IT:data_IT@apml.6w5pyjg.mongodb.net/")
db = client["Apml_calculator"]
collection = db["lcl_v9"]

class RateRequest(BaseModel):
    cft: float
    vehicleType: str
    pickupDistance: float
    dropDistance: float
    dismantleItems: List[List[float]]
    from_: str = Field(..., alias='from')
    to: str
    pickupDate: str
    declaredGoodsValue: float = 0
    employeeCode: str = ""
    enquiryNumber: str = ""

    class Config:
        allow_population_by_field_name = True
        populate_by_name = True
        json_encoders = {
            # Add any custom JSON encoders if needed
        }

def calculate_packing_charges(cft: float) -> float:
    # Find the correct slab rate
    slab_rate = None
    if cft <= 50:
        slab_rate = charges['cft0to50']['rate']
    elif 51 <= cft <= 100:
        slab_rate = charges['cft51to100']['rate']
    elif 101 <= cft <= 200:
        slab_rate = charges['cft101to200']['rate']
    elif 201 <= cft <= 350:
        slab_rate = charges['cft201to350']['rate']
    elif 351 <= cft <= 600:
        slab_rate = charges['cft351to600']['rate']
    else:  # 601-2000
        slab_rate = charges['cft601to2000']['rate']

    pc = cft * slab_rate

    # Apply minimum packing charge if needed
    if cft < charges['minimumUptoCft']:
        pc = max(pc, charges['minimumPacking'])

    return pc

def calculate_freight_cost(cft: float, vehicle_type: str, transport_rate: Dict, pickup_date: datetime) -> float:
    ist_date = pickup_date.astimezone(IST)  # Convert to IST first
    is_monday = ist_date.weekday() == 0     # Now check Monday in IST
    is_special_day = is_weekend(pickup_date) or is_monthend(pickup_date)
    
    if vehicle_type == "Full Truck Load":
        if cft <= 50:
            if is_monday:
                return transport_rate["Monday (FTL 501 to 800 cft)"]  # Adjust key as needed
            return transport_rate["Week days up to 50 cft"]
        elif 51 <= cft <= 800:
            if is_monday:
                return transport_rate["Monday (FTL 501 to 800 cft)"]
            elif is_special_day:
                return transport_rate["Weekend/Monthend (FTL 501 to 800 cft)"]
            return transport_rate["FTL 501 to 800 cft Week days"]
        elif 801 <= cft <= 1200:
            if is_monday:
                return transport_rate["Monday (801 to 1200 cft)"]
            elif is_special_day:
                return transport_rate["Weekend/Monthend (801 to 1200 cft)"]
            return transport_rate["801 to 1200 cft"]
        elif 1201 <= cft <= 1600:
            if is_monday:
                return transport_rate["Monday (1201 to 1600 cft 110%)"]
            elif is_special_day:
                return transport_rate["Weekend/Monthend (1201 to 1600 cft 110%)"]
            return transport_rate["1201 to 1600 cft 110%"]
        elif 1601 <= cft <= 2000:
            if is_monday:
                return transport_rate["Monday (1601 to 2000 cft 120%)"]
            elif is_special_day:
                return transport_rate["Weekend/Monthend (1601 to 2000 cft 120%)"]
            return transport_rate["1601 to 2000 cft 120%"]
    else:  # Shared Vehicle
        if cft <= 50:
            if is_monday:
                return transport_rate["Monday up to 50 days"]
            elif is_special_day:
                return transport_rate["Weekend and month end up to 50 cft"]
            return transport_rate["Week days up to 50 cft"]
        elif 51 <= cft <= 500:
            if is_monday:
                rate = transport_rate["Per cft Monday (from 51 cft to 500 cft)"]
            elif is_special_day:
                rate = transport_rate["Per cft Weekend and month end (from 51 cft to 500 cft)"]
            else:
                rate = transport_rate["Per cft week days (from 51 cft to 500 cft)"]
            return rate * cft
        else:
            rate = transport_rate["Per cft Weekend and month end (from 51 cft to 500 cft)"]
            return rate * cft

def calculate_extra_freight_cost(cft: float, pickup_distance: float, drop_distance: float) -> float:
    if pickup_distance <= 30 and drop_distance <= 30:
        return 0

    pickup_distance = 0 if pickup_distance <= 30 else pickup_distance
    drop_distance = 0 if drop_distance <= 30 else drop_distance

    cft_keys = []
    for key, _ in charges.items():
        if isinstance(key, str):
            range_values = [int(x) for x in ''.join(c if c.isdigit() else ' ' for c in key).split()]
            if range_values:
                cft_keys.append([*range_values, key])

    cft_key = get_cft_key_for_number(cft, cft_keys)
    if cft_key == -1:
        return 0

    pickup_cost = pickup_distance * charges[cft_key]['delivery']
    drop_cost = drop_distance * charges[cft_key]['delivery']

    return pickup_cost + drop_cost

def calculate_dismantle_cost(dismantle_items: List[List[float]]) -> float:
    return sum(rate * count for rate, count in dismantle_items)

def is_weekend(date: datetime) -> bool:
    """Check weekend in IST timezone"""
    ist_date = date.astimezone(IST)
    return ist_date.weekday() >= 4  # Fri(4), Sat(5), Sun(6)

def is_monthend(date: datetime) -> bool:
    """Check monthend in IST timezone"""
    ist_date = date.astimezone(IST)
    return ist_date.day >= 25 or ist_date.day <= 2

def is_holiday(date_str: str) -> bool:
    return any(holiday[0] == date_str for holiday in cap_wise_rates.get('holidays', []))

def format_date(date: datetime) -> str:
    """Format date in IST timezone"""
    return date.astimezone(IST).strftime("%d/%m/%y")

def get_future_six_days(date: Optional[datetime] = None) -> List[datetime]:
    start_date = date or datetime.now(timezone.utc)
    return [start_date.astimezone(timezone.utc) + timedelta(days=i) for i in range(6)]

def get_day_type(date: datetime) -> str:
    """Get day type using IST timezone"""
    ist_date = date.astimezone(IST)
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    day_abbrev = days[ist_date.weekday()]
    
    # Build day type components
    day_type = []
    if is_weekend(ist_date):
        day_type.append("Weekend")
    else:
        day_type.append("Weekday")
    
    if is_monthend(ist_date):
        day_type.append("Monthend")
    
    return f"{' '.join(day_type)} ({day_abbrev})"

def get_rate(request: RateRequest, transport_rate: Dict, pickup_date: datetime) -> Dict:
    formatted_pickup_date = format_date(pickup_date)

    packing_cost = calculate_packing_charges(request.cft)
    freight_cost = calculate_freight_cost(request.cft, request.vehicleType, transport_rate, pickup_date)
 


    extra_freight_cost = calculate_extra_freight_cost(
            request.cft, 
            request.pickupDistance, 
            request.dropDistance
        )
    dismantle_cost = calculate_dismantle_cost(request.dismantleItems)
    
    # # Apply March multiplier
    # if pickup_date.month == 3:  # March
    #     freight_cost *= 1.05
        
    # freight_cost = apply_discount(freight_cost, pickup_date)
    
    # Calculate total and surcharge
    total = (packing_cost + freight_cost + extra_freight_cost + 
            (float(request.declaredGoodsValue) * 0.03)+ dismantle_cost)
    surcharge = round(total * 0.0)
    
    return {
        "packingCost": round(packing_cost),
        "freightCost": round(freight_cost),
        "extraFreightCost": round(extra_freight_cost),
        "dismantleCost": dismantle_cost,
        "total": round(total + surcharge),
        "surcharge": surcharge,
        "dgv": (float(request.declaredGoodsValue) * 0.03)
    }

def get_future_rates(request: RateRequest, transport_rate: Dict, pickup_date: datetime) -> List[Dict]:
    results = []
    formatted_pickup_date = format_date(pickup_date)
    
    future_six_days = get_future_six_days()
    formatted_future_days = [format_date(day) for day in future_six_days]

    if formatted_pickup_date in formatted_future_days:
        future_days = get_future_six_days(pickup_date)
        
        for day in future_days:
            rate = get_rate(request, transport_rate, day)
            formatted_day = format_date(day)
            
            result = {
                "pickupDate": formatted_day,
                **rate,
                "dayType": get_day_type(day)
            }
            
            if formatted_day == formatted_pickup_date:
                result["isPickup"] = True
                
            results.append(result)
    else:
        date = pickup_date - timedelta(days=2)
        packing_days = get_future_six_days(date)
        
        for day in packing_days:
            rate = get_rate(request, transport_rate, day)
            formatted_day = format_date(day)
            
            result = {
                "pickupDate": formatted_day,
                **rate,
                "dayType": get_day_type(day)
            }
            
            if formatted_day == formatted_pickup_date:
                result["isPickup"] = True
                
            results.append(result)
    
    return results

def get_cft_key_for_number(number: float, ranges_array: List) -> str:
    for range_values in ranges_array:
        if len(range_values) >= 3 and range_values[0] <= number <= range_values[1]:
            return range_values[2]
    return -1

# def apply_discount(amount: float, date: datetime) -> float:
#     """Apply 6% discount if date is between 7th and 22nd and is a weekday"""
#     if 7 <= date.day <= 22 and 0 <= date.weekday() <= 4:
#         return math.ceil(amount * 0.94)
#     return amount

async def fetch_transport_locations():
    """Fetch all required data from external APIs"""
    global max_capacity, cap_wise_rates, weekend_locations, weekday_locations, charges
    
    try:
        # Create tasks for all API calls
        tasks = [
            asyncio.create_task(fetch_weekday_rates()),
            asyncio.create_task(fetch_weekend_rates()),
        ]
        
        # Wait for all tasks to complete
        responses = await asyncio.gather(*tasks)
        
        weekday_response, weekend_response = responses
        
        # # Debug: Print the raw responses
        # print("Weekend Response:", weekend_response)
        # print("Weekday Response:", weekday_response)
        
        # Update global variables
        weekend_locations = [{
            'FROM': row['FROM BRANCHES'].strip(),
            'TO': row['TO Station'].strip(),
            'Week days up to 50 cft': row['Week days up to 50 cft'],
            'Monday up to 50 days': row['Monday up to 50 days'],
            'Weekend and month end up to 50 cft': row['weekend and month end up to 50 cft'],
            'Per cft week days (from 51 cft to 500 cft)': row['Per cft week days (from 51 cft to 500 cft)'],
            'Per cft Monday (from 51 cft to 500 cft)': row['Per cft Monday (from 51 cft to 500 cft)'],
            'Per cft Weekend and month end (from 51 cft to 500 cft)': row['Per cft Weekend and month end (from 51 cft to 500 cft)'],
            'FTL 501 to 800 cft Week days': row['FTL 501 to 800 cft Week days'],
            'Monday (FTL 501 to 800 cft)': row['Monday (FTL 501 to 800 cft)'],
            'Weekend/Monthend (FTL 501 to 800 cft)': row['Weekend/Monthend (FTL 501 to 800 cft)'],
            '801 to 1200 cft': row['801 to 1200 cft'],
            'Monday (801 to 1200 cft)': row['Monday (801 to 1200 cft)'],
            'Weekend/Monthend (801 to 1200 cft)': row['Weekend/Monthend (801 to 1200 cft)'],
            '1201 to 1600 cft 110%': row['1201 to 1600 cft 110%'],
            'Monday (1201 to 1600 cft 110%)': row['Monday (1201 to 1600 cft 110%)'],
            'Weekend/Monthend (1201 to 1600 cft 110%)': row['Weekend/Monthend (1201 to 1600 cft 110%)'],
            '1601 to 2000 cft 120%': row['1601 to 2000 cft 120%'],
            'Monday (1601 to 2000 cft 120%)': row['Monday (1601 to 2000 cft 120%)'],
            'Weekend/Monthend (1601 to 2000 cft 120%)': row['Weekend/Monthend (1601 to 2000 cft 120%)']
        } for row in weekend_response['data']]


        weekday_locations = [{
            'FROM': row['FROM BRANCHES'].strip(),
            'TO': row['TO Station'].strip(),
            'Week days up to 50 cft': row['Week days up to 50 cft'],
            'Monday up to 50 days': row['Monday up to 50 days'],
            'Weekend and month end up to 50 cft': row['weekend and month end up to 50 cft'],
            'Per cft week days (from 51 cft to 500 cft)': row['Per cft week days (from 51 cft to 500 cft)'],
            'Per cft Monday (from 51 cft to 500 cft)': row['Per cft Monday (from 51 cft to 500 cft)'],
            'Per cft Weekend and month end (from 51 cft to 500 cft)': row['Per cft Weekend and month end (from 51 cft to 500 cft)'],
            'FTL 501 to 800 cft Week days': row['FTL 501 to 800 cft Week days'],
            'Monday (FTL 501 to 800 cft)': row['Monday (FTL 501 to 800 cft)'],
            'Weekend/Monthend (FTL 501 to 800 cft)': row['Weekend/Monthend (FTL 501 to 800 cft)'],
            '801 to 1200 cft': row['801 to 1200 cft'],
            'Monday (801 to 1200 cft)': row['Monday (801 to 1200 cft)'],
            'Weekend/Monthend (801 to 1200 cft)': row['Weekend/Monthend (801 to 1200 cft)'],
            '1201 to 1600 cft 110%': row['1201 to 1600 cft 110%'],
            'Monday (1201 to 1600 cft 110%)': row['Monday (1201 to 1600 cft 110%)'],
            'Weekend/Monthend (1201 to 1600 cft 110%)': row['Weekend/Monthend (1201 to 1600 cft 110%)'],
            '1601 to 2000 cft 120%': row['1601 to 2000 cft 120%'],
            'Monday (1601 to 2000 cft 120%)': row['Monday (1601 to 2000 cft 120%)'],
            'Weekend/Monthend (1601 to 2000 cft 120%)': row['Weekend/Monthend (1601 to 2000 cft 120%)']
        } for row in weekday_response['data']]        
        
        charges = weekday_response['charges']
        charges = weekend_response['charges']
        
    except Exception as e:
        print(f"Error fetching transport locations: {str(e)}")
        raise

async def fetch_weekday_rates():
    """Fetch weekday rates from external API"""
    response = await make_request(
        "https://script.google.com/macros/s/AKfycbxUobjEcJu894vxKV9O5hT4njqcParamaN_oCtk82FAHpS-iLwsPyPfR8HPH5pwSu2n/exec?action=getUser"
    )
    return response

async def fetch_weekend_rates():
    """Fetch weekend rates from external API"""
    response = await make_request(
        "https://script.google.com/macros/s/AKfycbxUobjEcJu894vxKV9O5hT4njqcParamaN_oCtk82FAHpS-iLwsPyPfR8HPH5pwSu2n/exec?action=getUser"
    )
    return response

async def make_request(url: str) -> Dict:
    """Make HTTP request with error handling"""
    try:
        response = requests.get(url)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        print(f"Error making request to {url}: {str(e)}")
        raise

# FastAPI routes
@app.on_event("startup")
async def startup_event():
    """Initialize data on startup"""
    await fetch_transport_locations()
    # Schedule periodic updates
    asyncio.create_task(schedule_updates())

async def schedule_updates():
    """Schedule periodic updates of transport locations"""
    while True:
        await asyncio.sleep(30 * 60)  # 30 minutes
        await fetch_transport_locations()

@app.post("/calculate-rate")
@app.post("/lclv9/calculate-rate")
async def calculate_rate(request: RateRequest):
    """Calculate transportation rates"""
    try:
        # Parse with IST timezone
        pickup_date = datetime.strptime(
            request.pickupDate.split('T')[0], 
            "%Y-%m-%d"
        ).replace(tzinfo=IST)
        
        # Ensure all date operations use IST
        future_six_days = get_future_six_days(pickup_date)
        formatted_future_days = [format_date(day) for day in future_six_days]
        
        # Check if date is too far in the future (e.g., more than 2 years)
        max_future_date = datetime.now(timezone.utc) + timedelta(days=730)  # 2 years
        if pickup_date > max_future_date:
            raise HTTPException(
                status_code=400, 
                detail="Pickup date cannot be more than 2 years in the future"
            )
            
        # Determine which locations to use
        is_weekend_rate = (
            is_weekend(pickup_date) or 
            is_monthend(pickup_date) or 
            pickup_date.day <= 2
        )
        transport_locations = weekend_locations if is_weekend_rate else weekday_locations

        # # Debug logging
        # print(f"Request from: {request.from_}, to: {request.to}")
        # print(f"Using {'weekend' if is_weekend_rate else 'weekday'} rates")
        # print(f"Available locations: {[{'FROM': loc['FROM'], 'TO': loc['TO']} for loc in transport_locations]}")

        # Find matching transport rate
        transport_rate = next(
            (rate for rate in transport_locations 
            if rate["FROM"] == request.from_ and rate["TO"] == request.to),
            None
        )

        if not transport_rate:
            raise HTTPException(
                status_code=400, 
                detail=f"No route found from {request.from_} to {request.to}"
            )

        # Calculate rates
        rates = get_future_rates(request, transport_rate, pickup_date)
        
        # Prepare response
        response_data = {
            "request": request.dict(),
            "response": {
                "rates": rates,
                "updatedAt": datetime.now().isoformat()
            },
            "timestamp": datetime.now()
        }
        
        # Save to MongoDB
        try:
            collection.insert_one(response_data)
        except Exception as e:
            print(f"Failed to save to MongoDB: {e}")
        
        return response_data['response']

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/locations")
@app.get("/lclv9/locations")

async def get_transport_locations():
    """Get available transport locations"""
    try:
        weekend_from: Set[str] = {loc["FROM"] for loc in weekend_locations}
        weekend_to: Set[str] = {loc["TO"] for loc in weekend_locations}
        weekday_from: Set[str] = {loc["FROM"] for loc in weekday_locations}
        weekday_to: Set[str] = {loc["TO"] for loc in weekday_locations}

        return {
            "status": "success",
            "weekend": {
                "from": list(weekend_from),
                "to": list(weekend_to)
            },
            "weekday": {
                "from": list(weekday_from),
                "to": list(weekday_to)
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=2323)
