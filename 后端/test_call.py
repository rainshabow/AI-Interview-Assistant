import sys
from pathlib import Path

# Ensure backend package dir is on sys.path
sys.path.insert(0, str(Path(__file__).parent))
import main


def run():
    try:
        req = main.RegisterReq(username='apitest', email='a@local', password='pass123')
        res = main.register(req)
        print('REGISTER_RES:', res)
    except Exception:
        import traceback
        traceback.print_exc()


if __name__ == '__main__':
    run()
