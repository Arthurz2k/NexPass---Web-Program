import os
import csv
import io
import re
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
import requests
import psycopg2
from psycopg2.extras import RealDictCursor

app = Flask(__name__)
app.secret_key = 'chave_secreta_nexpass'

# --- CHAVES DE ACESSO DO OPEN FINANCE - API ---
PLUGGY_CLIENT_ID = 'ee3eb89c-02c1-4099-b786-dd0d4c1fcf71'
PLUGGY_CLIENT_SECRET = 'dfc086ab-2d06-4441-8517-7871fe3293e0'

# --- CHAVE DO BANCO DE DADOS NA NUVEM (NEON/POSTGRESQL) ---
DATABASE_URL = "postgresql://neondb_owner:npg_6EdTfvkQia7r@ep-summer-dream-acyizfpg.sa-east-1.aws.neon.tech/neondb?sslmode=require"

def format_brl(value):
    return f"R$ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

def get_db_connection():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS usuarios (
            id SERIAL PRIMARY KEY, 
            nome TEXT, 
            email TEXT UNIQUE, 
            senha TEXT
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS transacoes (
            id SERIAL PRIMARY KEY, 
            usuario_id INTEGER REFERENCES usuarios(id), 
            descricao TEXT, 
            valor REAL, 
            tipo TEXT, 
            categoria TEXT, 
            data TEXT,
            banco TEXT DEFAULT 'Manual'
        )
    ''')
    conn.commit()
    cur.close()
    conn.close()

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/cadastro', methods=['GET', 'POST'])
def cadastro():
    if request.method == 'POST':
        nome = request.form.get('nome', '').strip()
        email = request.form.get('email', '').strip().lower() 
        senha = request.form.get('senha', '')
        confirmar_senha = request.form.get('confirmar_senha', '')

        if senha != confirmar_senha:
            flash('Erro: As senhas não coincidem.', 'error')
            return render_template('cadastro.html')

        padrao_senha = r"^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*[@$!%*?&])[A-Za-z\d@$!%*?&]{8,}$"
        if not re.match(padrao_senha, senha):
            flash('Erro: Senha fraca. Siga as regras de segurança.', 'error')
            return render_template('cadastro.html')

        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute('INSERT INTO usuarios (nome, email, senha) VALUES (%s, %s, %s)', (nome, email, senha))
            conn.commit()
            flash('Conta criada com sucesso! Você já pode acessar.', 'success')
            return redirect(url_for('login'))
        except psycopg2.IntegrityError:
            conn.rollback()
            flash('Erro: Este e-mail já está em uso.', 'error')
        finally: 
            cur.close()
            conn.close()
            
    return render_template('cadastro.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email, senha = request.form['email'], request.form['senha']
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('SELECT * FROM usuarios WHERE email = %s AND senha = %s', (email, senha))
        user = cur.fetchone()
        cur.close()
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
    cur = conn.cursor()

    cur.execute('SELECT * FROM usuarios WHERE id = %s', (user_id,))
    user_data = cur.fetchone()

    filtro_tipo = request.args.get('filtro_tipo', 'Todos')
    filtro_categoria = request.args.get('filtro_categoria', 'Todas')
    filtro_banco = request.args.get('filtro_banco', 'Todos')

    query = "SELECT * FROM transacoes WHERE usuario_id = %s"
    params = [user_id]

    if filtro_tipo != 'Todos':
        query += " AND LOWER(tipo) = LOWER(%s)"
        params.append(filtro_tipo)
    
    if filtro_categoria != 'Todas':
        query += " AND categoria = %s"
        params.append(filtro_categoria)
        
    if filtro_banco != 'Todos':
        query += " AND banco = %s"
        params.append(filtro_banco)

    query += " ORDER BY data DESC"
    
    cur.execute(query, tuple(params))
    transacoes = cur.fetchall()

    cur.execute('SELECT DISTINCT banco FROM transacoes WHERE usuario_id = %s', (user_id,))
    db_bancos = cur.fetchall()
    all_bancos = [row['banco'] if row['banco'] else 'Manual' for row in db_bancos]
    all_bancos = list(set(all_bancos))
    all_bancos.sort()

    receitas = 0
    despesas = 0

    cur.execute('SELECT * FROM transacoes WHERE usuario_id = %s', (user_id,))
    todas_transacoes = cur.fetchall()
    
    for t in todas_transacoes:
        tipo = str(t['tipo']).strip().title() 
        if tipo in ['Receita', 'Entrada']:
            receitas += t['valor']
        elif tipo in ['Despesa', 'Saída', 'Saida']:
            despesas += t['valor']

    ent = receitas
    sai = despesas

    cur.execute('''
        SELECT categoria, ABS(SUM(valor)) as total FROM transacoes 
        WHERE usuario_id = %s AND LOWER(TRIM(tipo)) IN ('despesa', 'saída', 'saida')
        GROUP BY categoria
    ''', (user_id,))
    dados_grafico = cur.fetchall()

    labels_cat = [d['categoria'] for d in dados_grafico]
    valores_cat = [float(d['total']) for d in dados_grafico]
    
    default_cats = ['Alimentação', 'Moradia', 'Transporte', 'Lazer', 'Saúde', 'Salário', 'Outros']
    cur.execute('SELECT DISTINCT categoria FROM transacoes WHERE usuario_id = %s', (user_id,))
    db_cats = cur.fetchall()
    user_cats = [row['categoria'] for row in db_cats if row['categoria']]
    all_cats = list(set(default_cats + user_cats))
    all_cats.sort()
    
    info = {"nome": session.get('user_nome', 'Usuário'), "saldo": format_brl(ent-sai), "entradas": format_brl(ent), "saidas": format_brl(sai)}
   
    cur.close()
    conn.close()
    
    return render_template('dashboard.html', 
                           info=info,
                           transacoes=transacoes, 
                           ent=ent, 
                           sai=sai, 
                           saldo=ent-sai, 
                           labels_cat=labels_cat, 
                           valores_cat=valores_cat, 
                           all_cats=all_cats,
                           filtro_tipo=filtro_tipo,
                           filtro_categoria=filtro_categoria,  
                           filtro_banco=filtro_banco,          
                           all_bancos=all_bancos)              
    
@app.route('/importar_csv', methods=['POST'])
def importar_csv():
    # Mantive a rota, mas a lógica completa depende de regras de CSV. 
    # Por padrão, ela redireciona para evitar quebra ao clicar.
    return redirect(url_for('dashboard'))

@app.route('/add_transacao', methods=['POST'])
def add_transacao():
    if 'user_id' in session:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('INSERT INTO transacoes (usuario_id, descricao, valor, tipo, categoria, data, banco) VALUES (%s,%s,%s,%s,%s,%s,%s)',
                     (session['user_id'], request.form['descricao'], abs(float(request.form['valor'])), 
                      request.form['tipo'], request.form['categoria'].strip(), request.form['data'], 'Manual'))
        conn.commit()
        cur.close()
        conn.close()
        flash('Transação salva!', 'success')
    return redirect(url_for('dashboard'))

@app.route('/edit_transacao', methods=['POST'])
def edit_transacao():
    if 'user_id' not in session: return redirect(url_for('login'))
    conn = get_db_connection()
    cur = conn.cursor()
    id_transacao = request.form['id']
    valor_limpo = abs(float(request.form['valor']))
    
    cur.execute('''
        UPDATE transacoes 
        SET descricao = %s, valor = %s, tipo = %s, categoria = %s, data = %s
        WHERE id = %s AND usuario_id = %s
    ''', (request.form['descricao'], valor_limpo, request.form['tipo'], 
          request.form['categoria'].strip(), request.form['data'], id_transacao, session['user_id']))
    conn.commit()
    cur.close()
    conn.close()
    flash('Transação atualizada com sucesso!', 'success')
    return redirect(url_for('dashboard'))

@app.route('/delete_transacao/<int:id>')
def delete_transacao(id):
    if 'user_id' not in session: return redirect(url_for('login'))
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('DELETE FROM transacoes WHERE id = %s AND usuario_id = %s', (id, session['user_id']))
    conn.commit()
    cur.close()
    conn.close()
    flash('Transação excluída com sucesso.', 'success')
    return redirect(url_for('dashboard'))

@app.route('/clear_transacoes', methods=['POST'])
def clear_transacoes():
    if 'user_id' not in session: return redirect(url_for('login'))
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('DELETE FROM transacoes WHERE usuario_id = %s', (session['user_id'],))
    conn.commit()
    cur.close()
    conn.close()
    flash('Todos os dados foram apagados!', 'success')
    return redirect(url_for('dashboard'))

# ==========================================
# INTEGRAÇÃO OPEN FINANCE (PLUGGY)
# ==========================================

@app.route('/gerar_token_pluggy')
def gerar_token_pluggy():
    if 'user_id' not in session: 
        return {"erro": "Usuário não logado"}, 401
        
    try:
        auth_req = requests.post(
            "https://api.pluggy.ai/auth",
            json={"clientId": PLUGGY_CLIENT_ID, "clientSecret": PLUGGY_CLIENT_SECRET}
        )
        api_key = auth_req.json().get("apiKey")
        
        token_req = requests.post(
            "https://api.pluggy.ai/connect_token",
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"options": {"clientUserId": str(session.get('user_id', 'teste'))}} 
        )
        
        return {"token": token_req.json().get("accessToken")}
    except Exception as e:
        return {"erro": str(e)}, 500

@app.route('/sincronizar_pluggy', methods=['POST'])
def sincronizar_pluggy():
    if 'user_id' not in session:
        return {"erro": "Usuário não logado"}, 401

    dados = request.get_json()
    item_id = dados.get('item_id')

    if not item_id:
        return {"erro": "ID do Item não fornecido"}, 400

    try:
        auth_req = requests.post(
            "https://api.pluggy.ai/auth",
            json={"clientId": PLUGGY_CLIENT_ID, "clientSecret": PLUGGY_CLIENT_SECRET}
        )
        api_key = auth_req.json().get("apiKey")
        headers = {"X-API-KEY": api_key}

        item_req = requests.get(f"https://api.pluggy.ai/items/{item_id}", headers=headers)
        nome_banco = item_req.json().get('connector', {}).get('name', 'Pluggy Bank')

        contas_req = requests.get(f"https://api.pluggy.ai/accounts?itemId={item_id}", headers=headers)
        contas = contas_req.json().get('results', [])

        conn = get_db_connection()
        cur = conn.cursor()
        transacoes_importadas = 0

        for conta in contas:
            conta_id = conta['id']
            transacoes_req = requests.get(f"https://api.pluggy.ai/transactions?accountId={conta_id}", headers=headers)
            transacoes = transacoes_req.json().get('results', [])

            for t in transacoes:
                descricao = t.get('description', 'Transação Automática')
                valor = abs(t.get('amount', 0))
                tipo = 'Receita' if t.get('type') == 'CREDIT' else 'Despesa'
                categoria = t.get('category', 'Outros')
                data_transacao = t.get('date', '')[:10]

                # Escudo Anti-Duplicidade do Postgres (%s)
                cur.execute('''
                    SELECT id FROM transacoes 
                    WHERE usuario_id = %s AND descricao = %s AND valor = %s AND data = %s
                ''', (session['user_id'], descricao, valor, data_transacao))
                
                if cur.fetchone():
                    continue 

                cur.execute('''
                    INSERT INTO transacoes (usuario_id, descricao, valor, tipo, categoria, data, banco)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                ''', (session['user_id'], descricao, valor, tipo, categoria, data_transacao, nome_banco))
                transacoes_importadas += 1

        conn.commit()
        cur.close()
        conn.close()

        return {"sucesso": True, "total": transacoes_importadas}

    except Exception as e:
        print(f"Erro na sincronização: {e}")
        return {"erro": str(e)}, 500
    
@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('home'))

@app.route('/cartoes')
def cartoes():
    if 'user_id' not in session: return redirect(url_for('login'))
    user_id = session['user_id']
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute('SELECT * FROM usuarios WHERE id = %s', (user_id,))
    info = cur.fetchone()

    cur.execute('''
        SELECT 
            banco,
            SUM(CASE WHEN LOWER(TRIM(tipo)) IN ('receita', 'entrada') THEN valor ELSE 0 END) as receitas,
            SUM(CASE WHEN LOWER(TRIM(tipo)) IN ('despesa', 'saída', 'saida') THEN valor ELSE 0 END) as despesas
        FROM transacoes 
        WHERE usuario_id = %s 
        GROUP BY banco
    ''', (user_id,))
    bancos_db = cur.fetchall()

    cur.close()
    conn.close()

    lista_bancos = []
    for b in bancos_db:
        nome_banco = b['banco'] if b['banco'] else 'Manual'
        rec = float(b['receitas'] or 0)
        des = float(b['despesas'] or 0)
        saldo = rec - des
        
        nome_imagem = nome_banco.lower().replace(' ', '_') + '.png'

        lista_bancos.append({
            'nome': nome_banco,
            'saldo': saldo,
            'receitas': rec,
            'despesas': des,
            'imagem': nome_imagem
        })

    return render_template('cartoes.html', info=info, bancos=lista_bancos)

if __name__ == '__main__':
    init_db()
    # Preparado para o Google Cloud Run!
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=True)