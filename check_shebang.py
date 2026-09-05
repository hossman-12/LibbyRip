import sys
p = r'c:\Users\cstep\OneDrive\Documents\git\LibbyRip\auto_build_m4b.py'
with open(p, 'rb') as f:
    data = f.read(64)
sys.stdout.write('first 64 bytes:\n')
sys.stdout.write(repr(data) + '\n')
sys.stdout.write('starts with shebang: ' + str(data.startswith(b'#!')) + '\n')
sys.stdout.write('has CR: ' + str(b'\r' in data) + '\n')
sys.stdout.write('first line repr: ' + repr(data.split(b'\n', 1)[0]) + '\n')
