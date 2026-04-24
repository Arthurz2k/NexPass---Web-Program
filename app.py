from flask import Flask, render_template, request, redirect, url_for, session, flash
import sqlite3

app = Flask(__name__)
app.secret_key = 'chave_secreta_nexpass'

# Função para formatar dinheiro (ex: 1000 -> R$ 1.000,00)
def format_brl(value):
    return f"R$ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

def get_db_connection():
    conn = sqlite3.connect('nexpass.db')
    conn.row_factory = sqlite3.Row
    return conn

# Cria as tabelas se não existirem
def init_db():
    conn = get_db_connection()
    conn.execute('CREATE TABLE IF NOT EXISTS usuarios (id INTEGER PRIMARY KEY AUTOINCREMENT, nome TEXT, email TEXT UNIQUE, senha TEXT)')
    conn.execute('''CREATE TABLE IF NOT EXISTS transacoes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, usuario_id INTEGER, 
                    descricao TEXT, valor REAL, tipo TEXT, categoria TEXT, data TEXT)''')
    conn.commit()
    conn.close()

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/cadastro', methods=['GET', 'POST'])
def cadastro():
    if request.method == 'POST':
        nome, email, senha = request.form['nome'], request.form['email'], request.form['senha']
        conn = get_db_connection()
        try:
            conn.execute('INSERT INTO usuarios (nome, email, senha) VALUES (?, ?, ?)', (nome, email, senha))
            conn.commit()
            flash('Conta criada com sucesso! Faça login.', 'success')
            return redirect(url_for('login'))
        except:
            flash('Erro: E-mail já cadastrado.', 'error')
        finally: conn.close()
    return render_template('cadastro.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email, senha = request.form['email'], request.form['senha']
        conn = get_db_connection()
        user = conn.execute('SELECT * FROM usuarios WHERE email = ? AND senha = ?', (email, senha)).fetchone()
        conn.close()
        if user:
            session['user_id'], session['user_nome'] = user['id'], user['nome']
            return redirect(url_for('dashboard'))
        flash('E-mail ou senha incorretos.', 'error')
    return render_template('login.html')

@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session: return redirect(url_for('login'))
    user_id = session['user_id']
    conn = get_db_connection()
    transacoes = conn.execute('SELECT * FROM transacoes WHERE usuario_id = ? ORDER BY data DESC', (user_id,)).fetchall()
    ent = conn.execute('SELECT SUM(valor) FROM transacoes WHERE usuario_id=? AND tipo="receita"', (user_id,)).fetchone()[0] or 0
    sai = conn.execute('SELECT SUM(valor) FROM transacoes WHERE usuario_id=? AND tipo="despesa"', (user_id,)).fetchone()[0] or 0
    conn.close()
    
    info = {"nome": session['user_nome'], "saldo": format_brl(ent-sai), "entradas": format_brl(ent), "saidas": format_brl(sai)}
    return render_template('dashboard.html', info=info, transacoes=transacoes)

@app.route('/add_transacao', methods=['POST'])
def add_transacao():
    if 'user_id' in session:
        conn = get_db_connection()
        conn.execute('INSERT INTO transacoes (usuario_id, descricao, valor, tipo, categoria, data) VALUES (?,?,?,?,?,?)',
                     (session['user_id'], request.form['descricao'], float(request.form['valor']), 
                      request.form['tipo'], request.form['categoria'], request.form['data']))
        conn.commit()
        conn.close()
    return redirect(url_for('dashboard'))

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('home'))

if __name__ == '__main__':
    init_db()
    app.run(debug=True)