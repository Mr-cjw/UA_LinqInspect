import os
import shutil

dest = r'D:\UA_LinqInspect\.conda\Lib\site-packages'
src_wb = r'C:\Users\XunMengTong\.workbuddy\binaries\python\envs\default\Lib\site-packages'
src_qw = r'D:\miniconda3\envs\qwen_env\Lib\site-packages'

groups = {
    src_wb: [
        'asyncua', 'asyncua-2.1.0.dist-info',
        'aiosqlite', 'aiosqlite-0.22.1.dist-info',
        'anyio', 'anyio-4.15.1.dist-info',
        'dateutil', 'python_dateutil-4.0.0.dist-info',
        'pytz', 'pytz-2026.6.dist-info',
        'sortedcontainers', 'sortedcontainers-2.4.0.dist-info',
        'typing_extensions.py', 'typing_extensions-4.16.0.dist-info',
    ],
    src_qw: [
        'cryptography', 'cryptography-50.0.2.dist-info',
        'cffi', 'cffi-2.1.1.dist-info',
        'pycparser', 'pycparser-3.12.dist-info',
        'idna', 'idna-3.20.dist-info',
    ],
}

for src, entries in groups.items():
    for name in entries:
        s = os.path.join(src, name)
        d = os.path.join(dest, name)
        if not os.path.exists(s):
            raise FileNotFoundError(s)
        if os.path.isdir(s):
            shutil.copytree(s, d, dirs_exist_ok=True)
        else:
            shutil.copy2(s, d)
        print('copied', name)
print('DONE')
