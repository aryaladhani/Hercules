import pandas as pd
import numpy as np

class Signal():
    def __init__(self):
        pass 

    def generate_signal(self, data):
        path = '/Users/aryaladhani/Documents/Hercules/Data/Processed/Spread/SettleSpreads/CAL_Active2_2_SettleSpreads.parquet'
        
        df = pd.read_parquet(path)
        df.CurrentDate = pd.to_datetime(df.CurrentDate)
        df = df [df.CurrentDate <='20241231']
        
        