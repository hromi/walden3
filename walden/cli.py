from __future__ import annotations
import argparse, asyncio, json, logging
from .config import load_config
from .factory import build

async def _main(args):
    cfg=load_config(args.config)
    logging.basicConfig(level=getattr(logging,cfg.logging.level.upper(),logging.INFO),format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    agent=build(cfg)
    if args.command=="matrix":
        from .matrix_service import MatrixService
        await MatrixService(cfg,agent).run()
    elif args.command=="person": print(await agent.graph.ensure_person(args.id))
    elif args.command=="room": print(await agent.graph.ensure_room(args.id))
    elif args.command=="resolve": print(json.dumps(await agent.graph.resolve(args.uri),indent=2,ensure_ascii=False))
    elif args.command=="lineage": print(json.dumps(await agent.graph.lineage(args.uri,args.limit),indent=2,ensure_ascii=False))
    elif args.command=="api":
        import uvicorn
        from .api import make_app
        config=uvicorn.Config(make_app(cfg,agent.graph),host=cfg.api.host,port=cfg.api.port,log_level=cfg.logging.level.lower())
        await uvicorn.Server(config).serve()
    elif args.command=="self-test":
        p=await agent.graph.ensure_person("@alice:example.org"); r=await agent.graph.ensure_room("!room:example.org")
        await agent.graph.update_person(p,{"preferences":{"tea":"green"}},{"source":"self-test"})
        await agent.graph.update_person(p,{"projects":{"walden":"active"}},{"source":"self-test"})
        await agent.graph.update_room(r,{"topic":"Walden memory graph"},{"source":"self-test"})
        ep=await agent.graph.create_episode(room_ref=r,person_refs=[p],when={"start":"now","end":"now"},where={"kind":"Matrix"},what={"summary":"self-test"},source={"test":True})
        obj=await agent.graph.resolve(p); line=await agent.graph.lineage(p)
        assert obj["memory"]["preferences"]["tea"]=="green" and obj["memory"]["projects"]["walden"]=="active" and len(line)>=3
        print(json.dumps({"ok":True,"person":p,"room":r,"episode":ep,"lineage":len(line)},indent=2))

def main():
    ap=argparse.ArgumentParser(prog="walden"); ap.add_argument("-c","--config",default="config.yaml")
    sub=ap.add_subparsers(dest="command",required=True)
    sub.add_parser("matrix"); sub.add_parser("api"); sub.add_parser("self-test")
    p=sub.add_parser("person"); p.add_argument("id")
    p=sub.add_parser("room"); p.add_argument("id")
    p=sub.add_parser("resolve"); p.add_argument("uri")
    p=sub.add_parser("lineage"); p.add_argument("uri"); p.add_argument("--limit",type=int,default=50)
    asyncio.run(_main(ap.parse_args()))

if __name__=="__main__": main()
