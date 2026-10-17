from types import SimpleNamespace
import unittest
import test_monitor as base

class AllChatsTests(base.HandlerTests):
    async def test_scope_edit_delete_and_account_gate(self):
        base.load_tdlib_handler()(self.subject,self.bot,99,None,self.store,all_chats=True,excluded_chat_ids={876})
        for chat in (42,-100222,-333):
            await self.send_new(chat,20,7,'original')
            self.assertIsNotNone(self.store.get(chat_id=chat,message_id=20))
        for chat in (777000,876,0):
            await self.send_new(chat,20,7,'excluded')
            self.assertIsNone(self.store.get(chat_id=chat,message_id=20))
        await self.client.handlers['content'](self.client,SimpleNamespace(data={'chat_id':42,'message_id':20,'new_content':{'text':{'text':'changed'}}}))
        await self.client.handlers['deleted'](self.client,SimpleNamespace(data={'chat_id':-100222,'message_ids':[20],'is_permanent':True}))
        self.assertEqual(self.store.get(chat_id=42,message_id=20).message_text,'changed')
        self.assertIsNone(self.store.get(chat_id=-100222,message_id=20))
        self.assertEqual(self.store.get(chat_id=-333,message_id=20).message_text,'original')
        self.assertEqual(len(self.bot.sent),2)
        self.subject._tracking_ready=False
        await self.send_new(43,20,7,'unverified')
        self.assertIsNone(self.store.get(chat_id=43,message_id=20))

    async def test_channel_sender(self):
        base.load_tdlib_handler()(self.subject,self.bot,99,None,self.store,all_chats=True)
        await self.client.handlers['new'](self.client,SimpleNamespace(data={'message':{'chat_id':-100222,'id':21,'sender_id':{'@type':'messageSenderChat','chat_id':-100222},'content':{'text':{'text':'channel post'}}}}))
        self.assertEqual(self.store.get(chat_id=-100222,message_id=21).author_id,-100222)

if __name__=='__main__':unittest.main()
