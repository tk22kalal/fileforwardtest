#(©)CodeXBotz




import pymongo, os
from config import MONGO_URI, MONGO_NAME


dbclient = pymongo.MongoClient(MONGO_URI)
database = dbclient[MONGO_NAME]


user_data = database['users']
collection = database['token']


async def present_user(user_id : int):
    found = user_data.find_one({'_id': user_id})
    return bool(found)

async def adds_user(user_id: int):
    user_data.insert_one({'_id': user_id})
    return

async def fulls_userbase():
    user_docs = user_data.find()
    user_ids = []
    for doc in user_docs:
        user_ids.append(doc['_id'])
        
    return user_ids

async def del_user(user_id: int):
    user_data.delete_one({'_id': user_id})
    return
